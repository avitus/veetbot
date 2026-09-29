"""The Gmail server entry point refuses unsafe inputs before any server or browser starts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import gmail_mcp.__main__ as gmail_main
from gmail_mcp.bootstrap import BootstrapError
from gmail_mcp.constants import GOOGLE_SCOPES

CLIENT = {"client_id": "client-id", "client_secret": "-".join(("client", "value"))}


def _client_file(tmp_path: Path, document: object, mode: int = 0o600) -> Path:
    path = tmp_path / "oauth-client.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, mode)
    return path


@pytest.mark.parametrize("document", [CLIENT, {"installed": CLIENT}], ids=["flat", "installed"])
def test_an_owner_only_oauth_client_file_yields_its_client(
    tmp_path: Path, document: object
) -> None:
    assert gmail_main._oauth_client(_client_file(tmp_path, document)) == (
        CLIENT["client_id"],
        CLIENT["client_secret"],
    )


@pytest.mark.parametrize("mode", [0o640, 0o604])
def test_a_readable_oauth_client_file_is_refused(tmp_path: Path, mode: int) -> None:
    with pytest.raises(BootstrapError, match="owner-only"):
        gmail_main._oauth_client(_client_file(tmp_path, CLIENT, mode))


@pytest.mark.parametrize(
    "document",
    [
        {"client_id": "client-id"},
        {"installed": {"client_secret": CLIENT["client_secret"]}},
        {"client_id": "", "client_secret": CLIENT["client_secret"]},
        ["not", "an", "object"],
    ],
    ids=["no-secret", "no-id", "blank-id", "not-an-object"],
)
def test_an_incomplete_oauth_client_file_is_refused(tmp_path: Path, document: object) -> None:
    with pytest.raises(BootstrapError, match="invalid"):
        gmail_main._oauth_client(_client_file(tmp_path, document))


def test_a_relative_symlinked_or_unreadable_oauth_client_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _client_file(tmp_path, CLIENT)
    link = tmp_path / "linked.json"
    link.symlink_to(real)
    monkeypatch.chdir(tmp_path)
    for path in (Path("oauth-client.json"), link):
        with pytest.raises(BootstrapError, match="absolute private regular file"):
            gmail_main._oauth_client(path)
    with pytest.raises(BootstrapError, match="invalid"):
        gmail_main._oauth_client(tmp_path / "absent.json")
    real.write_text("not json", encoding="utf-8")
    with pytest.raises(BootstrapError, match="invalid"):
        gmail_main._oauth_client(real)


@pytest.fixture
def no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a refused invocation started a Gmail server")

    monkeypatch.setattr(gmail_main, "create_server", forbidden)
    monkeypatch.setattr(gmail_main, "bootstrap_credentials", forbidden)
    monkeypatch.delenv("GMAIL_MCP_CREDENTIAL", raising=False)
    monkeypatch.delenv("GMAIL_OAUTH_CLIENT_FILE", raising=False)


@pytest.mark.usefixtures("no_server")
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--mode", "read", "bootstrap"], "mutually exclusive"),
        (["bootstrap"], "requires GMAIL_OAUTH_CLIENT_FILE"),
        (["--mode", "read"], "gmail.credential_rejected"),
    ],
)
def test_the_entry_point_refuses_an_incomplete_invocation(argv: list[str], message: str) -> None:
    with pytest.raises(SystemExit) as refused:
        gmail_main.main(argv)
    assert message in str(refused.value.code)


@pytest.mark.usefixtures("no_server")
def test_a_credential_for_another_scope_never_starts_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GMAIL_MCP_CREDENTIAL",
        json.dumps({**CLIENT, "refresh_token": "fixture", "scope": GOOGLE_SCOPES["read"]}),
    )
    with pytest.raises(SystemExit) as refused:
        gmail_main.main(["--mode", "send"])
    assert str(refused.value.code).startswith("gmail.")


@pytest.mark.usefixtures("no_server")
def test_the_entry_point_requires_a_mode_or_bootstrap() -> None:
    with pytest.raises(SystemExit) as refused:
        gmail_main.main([])
    assert refused.value.code == 2


@pytest.mark.usefixtures("no_server")
def test_bootstrap_refuses_a_symlinked_client_file_before_any_consent(tmp_path: Path) -> None:
    """The runbook requires a non-symlink client file; resolving it first would hide the link."""
    link = tmp_path / "linked.json"
    link.symlink_to(_client_file(tmp_path, CLIENT))

    with pytest.raises(SystemExit) as refused:
        gmail_main.main(["bootstrap", "--client-file", str(link)])

    assert "absolute private regular file" in str(refused.value.code)
