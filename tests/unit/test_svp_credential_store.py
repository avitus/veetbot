"""The package keeps a rotating grant durable and shared across processes (ADR-0153)."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from svp_mcp.credential import CredentialStore, create_state_file, read_state
from svp_mcp.errors import SvpError

NOW = 1_800_000_000.0
SECRET = "-".join(("client", "value"))


def _state(**changes: object) -> dict[str, Any]:
    state: dict[str, Any] = {
        "version": 1,
        "client_id": "client-1",
        "client_secret": SECRET,
        "refresh_token": "refresh-1",
        "access_token": "access-1",
        "expires_at": NOW + 3600,
    }
    state.update(changes)
    return state


def _file(directory: Path, state: object, mode: int = 0o600) -> Path:
    path = directory / "svp.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(path, mode)
    return path


class Server:
    """A token endpoint that rotates the refresh token on every use."""

    def __init__(self, *, rotate: bool = True, status: int = 200, delay: float = 0.0) -> None:
        self.forms: list[dict[str, str]] = []
        self.rotate = rotate
        self.status = status
        self.delay = delay

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.forms.append(dict(parse_qsl(request.content.decode("ascii"))))
        time.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, json={"error_description": "private diagnostic"})
        number = len(self.forms) + 1
        grant: dict[str, object] = {
            "access_token": f"access-{number}",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        if self.rotate:
            grant["refresh_token"] = f"refresh-{number}"
        return httpx.Response(200, json=grant)


def _store(path: Path, server: Callable[[httpx.Request], httpx.Response]) -> CredentialStore:
    return CredentialStore(
        path, http=httpx.Client(transport=httpx.MockTransport(server)), clock=lambda: NOW
    )


def test_a_live_access_token_is_used_without_a_request(tmp_path: Path) -> None:
    server = Server()

    assert _store(_file(tmp_path, _state()), server).access_token() == "access-1"
    assert server.forms == []


@pytest.mark.parametrize("expires_at", [NOW - 1, NOW + 59], ids=["expired", "inside-the-skew"])
def test_an_expiring_token_is_refreshed_and_the_rotation_is_durable(
    tmp_path: Path, expires_at: float
) -> None:
    server = Server()
    path = _file(tmp_path, _state(expires_at=expires_at))

    assert _store(path, server).access_token() == "access-2"

    assert [form["refresh_token"] for form in server.forms] == ["refresh-1"]
    assert read_state(path) == _state(
        refresh_token="refresh-2", access_token="access-2", expires_at=NOW + 3600
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(item.name for item in tmp_path.iterdir()) == ["svp.json", "svp.json.lock"]


def test_a_server_that_does_not_rotate_keeps_its_refresh_token(tmp_path: Path) -> None:
    path = _file(tmp_path, _state(expires_at=NOW - 1))

    assert _store(path, Server(rotate=False)).access_token() == "access-2"
    assert read_state(path)["refresh_token"] == "refresh-1"


def test_another_process_rotation_is_adopted_instead_of_repeated(tmp_path: Path) -> None:
    server = Server()
    path = _file(tmp_path, _state())
    store = _store(path, server)
    assert store.access_token() == "access-1"

    path.write_text(
        json.dumps(_state(refresh_token="refresh-9", access_token="access-9")), encoding="utf-8"
    )

    assert store.access_token(rejected="access-1") == "access-9"
    assert server.forms == []


def test_a_rejected_current_token_forces_one_refresh(tmp_path: Path) -> None:
    server = Server()
    store = _store(_file(tmp_path, _state()), server)

    assert store.access_token(rejected="access-1") == "access-2"
    assert store.access_token() == "access-2"
    assert len(server.forms) == 1


def test_concurrent_holders_share_one_rotation(tmp_path: Path) -> None:
    server = Server(delay=0.05)
    path = _file(tmp_path, _state(expires_at=NOW - 1))
    tokens: list[str] = []

    def use() -> None:
        tokens.append(_store(path, server).access_token())

    threads = [threading.Thread(target=use) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert tokens == ["access-2"] * 4
    assert len(server.forms) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "svp.credential_rejected"), (503, "svp.provider_unavailable")],
)
def test_a_failed_refresh_leaves_the_grant_untouched(
    tmp_path: Path, status: int, code: str
) -> None:
    path = _file(tmp_path, _state(expires_at=NOW - 1))
    before = path.read_bytes()

    with pytest.raises(SvpError) as caught:
        _store(path, Server(status=status)).access_token()

    assert str(caught.value) == code
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["svp.json", "svp.json.lock"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_no_refresh_is_spent_when_the_rotation_could_not_be_saved(tmp_path: Path) -> None:
    server = Server()
    directory = tmp_path / "state"
    directory.mkdir()
    path = _file(directory, _state(expires_at=NOW - 1))
    os.chmod(directory, 0o500)
    try:
        with pytest.raises(SvpError, match=r"^svp\.credential_invalid$"):
            _store(path, server).access_token()
    finally:
        os.chmod(directory, 0o700)

    assert server.forms == []


@pytest.mark.parametrize(
    "state",
    [
        [],
        {},
        _state(version=2),
        _state(refresh_token=""),
        _state(expires_at="soon"),
        _state(extra="value"),
        {key: value for key, value in _state().items() if key != "client_secret"},
    ],
    ids=["array", "empty", "version", "no-refresh", "bad-expiry", "extra-key", "missing-key"],
)
def test_a_malformed_state_file_is_refused(tmp_path: Path, state: object) -> None:
    with pytest.raises(SvpError, match=r"^svp\.credential_invalid$"):
        _store(_file(tmp_path, state), Server()).access_token()


def test_a_state_file_others_can_read_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SvpError, match=r"^svp\.credential_invalid$"):
        _store(_file(tmp_path, _state(), mode=0o640), Server()).access_token()


def test_a_missing_or_linked_state_file_is_refused(tmp_path: Path) -> None:
    target = _file(tmp_path, _state())
    link = tmp_path / "link.json"
    link.symlink_to(target)

    for path in (tmp_path / "absent.json", link, Path("relative.json")):
        with pytest.raises(SvpError, match=r"^svp\.credential_invalid$"):
            _store(path, Server()).access_token()


def test_the_ceremony_file_is_created_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "private" / "svp.json"

    create_state_file(path, _state())

    assert read_state(path) == _state()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(SvpError, match=r"^svp\.credential_invalid$"):
        create_state_file(path, _state(refresh_token="refresh-2"))
    assert read_state(path)["refresh_token"] == "refresh-1"
