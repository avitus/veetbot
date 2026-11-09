"""Offline Calendar consent, grant persistence and disclosure checks."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from calendar_mcp.__main__ import _authorize_with_disclosure, _oauth_client
from calendar_mcp.bootstrap import BootstrapError, bootstrap_credentials
from calendar_mcp.client import SCOPES


async def test_bootstrap_requests_exact_scopes_and_writes_private_selection(tmp_path: Path) -> None:
    def authorize(mode: str, url: str) -> str:
        values = parse_qs(urlsplit(url).query)
        assert mode == "read"
        assert values["scope"] == [SCOPES]
        assert values["code_challenge_method"] == ["S256"]
        assert values["access_type"] == ["offline"]
        assert values["state"][0]
        return "code"

    def exchange(request: httpx.Request) -> httpx.Response:
        values = parse_qs(request.content.decode())
        assert values["grant_type"] == ["authorization_code"]
        assert values["code_verifier"][0]
        return httpx.Response(
            200,
            json={"refresh_token": "private-token", "scope": " ".join(reversed(SCOPES.split()))},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as http:
        (path,) = await bootstrap_credentials(
            client_id="client",
            client_secret="secret",
            calendar_ids=["primary"],
            output_directory=tmp_path,
            authorize=authorize,
            http_client=http,
        )
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["calendar_ids"] == ["primary"]
        with pytest.raises(BootstrapError, match="already exists"):
            await bootstrap_credentials(
                client_id="client",
                client_secret="secret",
                calendar_ids=["primary"],
                output_directory=tmp_path,
                authorize=authorize,
                http_client=http,
            )


def test_declining_disclosure_never_opens_google() -> None:
    def unexpected(mode: str, url: str) -> str:
        pytest.fail("Declined consent must not open the browser")

    with pytest.raises(BootstrapError, match="cancelled"):
        _authorize_with_disclosure(
            "read", "https://example.test", read_input=lambda prompt: "NO", authorize=unexpected
        )


def test_oauth_client_requires_private_file(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}}))
    path.chmod(0o644)
    with pytest.raises(BootstrapError, match="owner-only"):
        _oauth_client(path)
    path.chmod(0o600)
    assert _oauth_client(path) == ("id", "secret")


def test_loopback_rejects_wrong_state_before_accepting_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import webbrowser
    from typing import Any

    import calendar_mcp.__main__ as entry
    from calendar_mcp.bootstrap import _authorization_url

    statuses: list[int] = []
    closed: list[bool] = []

    class Server:
        def __init__(self, address: tuple[str, int], handler: Any) -> None:
            assert address == ("127.0.0.1", 8765)
            self.handler = handler
            self.calls = 0
            self.timeout = 0.0

        def handle_request(self) -> None:
            self.calls += 1
            handler = object.__new__(self.handler)
            state = "wrong" if self.calls == 1 else "expected"
            handler.path = f"/?state={state}&code=code-{self.calls}"
            handler.wfile = io.BytesIO()
            handler.send_response = statuses.append
            handler.send_header = lambda *args: None
            handler.end_headers = lambda: None
            handler.do_GET()

        def server_close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(entry, "HTTPServer", Server)
    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: True)
    url = _authorization_url(
        client_id="client",
        scope=SCOPES,
        redirect_uri="http://127.0.0.1:8765",
        state="expected",
        verifier="verifier",
    )
    assert entry._authorize_via_loopback("read", url) == "code-2"
    assert statuses == [400, 200]
    assert closed == [True]


@pytest.mark.parametrize("scope", ["https://www.googleapis.com/auth/calendar", ""])
async def test_unexpected_grant_never_creates_a_credential(tmp_path: Path, scope: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"refresh_token": "private-token", "scope": scope}
            )
        )
    ) as http:
        with pytest.raises(BootstrapError, match="exact grant"):
            await bootstrap_credentials(
                client_id="client",
                client_secret="secret",
                calendar_ids=["primary"],
                output_directory=tmp_path,
                authorize=lambda mode, url: "code",
                http_client=http,
            )
    assert not (tmp_path / "calendar-read.json").exists()
