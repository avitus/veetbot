"""The bridge entry point takes its grant only as a private file path (ADR-0152)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import svp_mcp.__main__ as svp_main
from svp_mcp.constants import CREDENTIAL_VARIABLE

STATE = {
    "version": 1,
    "client_id": "client-1",
    "client_secret": "-".join(("client", "value")),
    "refresh_token": "refresh-1",
    "access_token": "access-1",
    "expires_at": 1_800_000_000,
}


class Recorded:
    def __init__(self) -> None:
        self.transports: list[str] = []

    def run(self, *, transport: str) -> None:
        self.transports.append(transport)


def _state_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "svp.json"
    path.write_text(json.dumps(STATE), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_the_server_starts_on_stdio_and_drops_the_path_from_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = Recorded()
    stores: list[Any] = []

    def create(client: Any) -> Recorded:
        stores.append(client)
        return recorded

    monkeypatch.setattr(svp_main, "create_server", create)
    monkeypatch.setenv(CREDENTIAL_VARIABLE, str(_state_file(tmp_path)))

    svp_main.main(["--mode", "read"])

    assert recorded.transports == ["stdio"]
    assert len(stores) == 1
    assert CREDENTIAL_VARIABLE not in os.environ


@pytest.mark.parametrize("value", [None, "", "relative/svp.json"])
def test_a_missing_or_relative_path_stops_the_server(
    value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svp_main, "create_server", lambda _client: Recorded())
    if value is None:
        monkeypatch.delenv(CREDENTIAL_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(CREDENTIAL_VARIABLE, value)

    with pytest.raises(SystemExit) as stopped:
        svp_main.main(["--mode", "read"])

    assert stopped.value.code == "svp.configuration_invalid"


def test_an_unreadable_grant_stops_the_server_before_it_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = Recorded()
    monkeypatch.setattr(svp_main, "create_server", lambda _client: recorded)
    monkeypatch.setenv(CREDENTIAL_VARIABLE, str(_state_file(tmp_path, mode=0o644)))

    with pytest.raises(SystemExit) as stopped:
        svp_main.main(["--mode", "read"])

    assert stopped.value.code == "svp.configuration_invalid"
    assert recorded.transports == []


def test_a_mode_is_required_and_only_read_exists() -> None:
    for arguments in ([], ["--mode", "write"]):
        with pytest.raises(SystemExit) as stopped:
            svp_main.main(arguments)
        assert stopped.value.code == 2


def test_bootstrap_refuses_a_mode_or_an_existing_output(tmp_path: Path) -> None:
    existing = _state_file(tmp_path)

    for arguments in (
        ["--mode", "read", "bootstrap", "--output-file", str(tmp_path / "new.json")],
        ["bootstrap", "--output-file", str(existing)],
        ["bootstrap", "--output-file", "relative.json"],
    ):
        with pytest.raises(SystemExit) as stopped:
            svp_main.main(arguments)
        assert stopped.value.code == "bootstrap requires a new absolute private-file path"
