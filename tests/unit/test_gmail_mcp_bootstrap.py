"""The bootstrap consent ceremony: three consents, three owner-only files."""

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from gmail_mcp.bootstrap import (
    GOOGLE_AUTHORIZATION_ENDPOINT,
    SERVER_GOOGLE_SCOPES,
    run_bootstrap,
)
from gmail_mcp.credential import TOKEN_ENDPOINT, GmailCredential

CLIENT_ID = "client-id-77.apps.googleusercontent.com"
CLIENT_SECRET = "client-secret-i3q9"


def _ceremony(
    tmp_path: Path,
) -> tuple[int, list[str], list[httpx.Request], dict[str, str]]:
    exchanges: list[httpx.Request] = []
    granted: dict[str, str] = {}
    printed: list[str] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        exchanges.append(request)
        form = parse_qs(request.read().decode())
        code = form["code"][0]
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{code}",
                "refresh_token": f"refresh-{code}",
                "expires_in": 3599,
            },
        )

    def grant(build_url: Callable[[str], str], state: str) -> tuple[str, str, str]:
        redirect_uri = "http://127.0.0.1:7777"
        url = build_url(redirect_uri)
        query = parse_qs(urlsplit(url).query)
        scope = query["scope"][0]
        granted[scope] = url
        assert query["redirect_uri"] == [redirect_uri]
        return f"code-{len(granted)}", state, redirect_uri

    status = run_bootstrap(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        output_directory=tmp_path,
        http=httpx.Client(transport=httpx.MockTransport(token_endpoint)),
        grant_source=grant,
        printer=printed.append,
    )
    return status, printed, exchanges, granted


def test_the_ceremony_writes_three_owner_only_round_tripping_files(tmp_path: Path) -> None:
    status, _printed, _exchanges, _granted = _ceremony(tmp_path)
    assert status == 0

    for server_id, scope in SERVER_GOOGLE_SCOPES.items():
        path = tmp_path / f"{server_id}.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        credential = GmailCredential.parse(path.read_text(encoding="utf-8"))
        assert credential.client_id == CLIENT_ID
        assert credential.scope == scope
        assert credential.refresh_token.startswith("refresh-code-")
        assert "\n" not in path.read_text(encoding="utf-8").strip()


def test_each_consent_requests_exactly_its_server_scope(tmp_path: Path) -> None:
    _status, _printed, exchanges, granted = _ceremony(tmp_path)

    assert set(granted) == set(SERVER_GOOGLE_SCOPES.values())
    for url in granted.values():
        parts = urlsplit(url)
        assert url.startswith(GOOGLE_AUTHORIZATION_ENDPOINT)
        query = parse_qs(parts.query)
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]
        assert query["code_challenge_method"] == ["S256"]
        assert len(query["scope"]) == 1 and " " not in query["scope"][0]
    assert len(exchanges) == 3
    for request in exchanges:
        assert str(request.url) == TOKEN_ENDPOINT
        form = parse_qs(request.read().decode())
        assert form["grant_type"] == ["authorization_code"]
        assert "code_verifier" in form


def test_the_ceremony_prints_paths_and_scopes_but_never_token_material(
    tmp_path: Path,
) -> None:
    _status, printed, _exchanges, _granted = _ceremony(tmp_path)

    output = "\n".join(printed)
    for server_id, scope in SERVER_GOOGLE_SCOPES.items():
        assert str(tmp_path / f"{server_id}.json") in output
        assert scope in output
    assert "refresh-code-" not in output
    assert "access-code-" not in output
    assert CLIENT_SECRET not in output


def test_the_files_round_trip_through_the_settings_loader(tmp_path: Path) -> None:
    from agent_core.config import load_settings
    from tests.unit.test_config import base_environment

    _ceremony(tmp_path)
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "AGENT_EMAIL_ENABLED": "1",
            **{
                f"{server_id.upper()}_CREDENTIAL_FILE": str(tmp_path / f"{server_id}.json")
                for server_id in SERVER_GOOGLE_SCOPES
            },
        }
    )
    for server_id in SERVER_GOOGLE_SCOPES:
        document = settings.credentials[server_id].get_secret_value()
        assert json.loads(document)["client_id"] == CLIENT_ID


def test_a_state_mismatch_aborts_without_writing(tmp_path: Path) -> None:
    def bad_grant(build_url: Callable[[str], str], state: str) -> tuple[str, str, str]:
        redirect_uri = "http://127.0.0.1:7777"
        build_url(redirect_uri)
        return "code-x", "tampered-state", redirect_uri

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no exchange may follow a tampered state")

    with pytest.raises(ValueError, match="state"):
        run_bootstrap(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            output_directory=tmp_path,
            http=httpx.Client(transport=httpx.MockTransport(token_endpoint)),
            grant_source=bad_grant,
            printer=lambda line: None,
        )
    assert not list(tmp_path.glob("*.json"))
