"""The one-time operator consent ceremony: three consents, three files.

Each server gets its own installed-app authorization-code consent, requesting
exactly that server's Google scope with PKCE and a state check, and its own
owner-only credential file. Paths and scopes are printed; token material and
the client secret never are. The default grant source runs a loopback
listener and opens a browser; tests inject their own.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import webbrowser
from base64 import urlsafe_b64encode
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from gmail_mcp.credential import TOKEN_ENDPOINT

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
SERVER_GOOGLE_SCOPES: dict[str, str] = {
    "gmail_read": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail_write": "https://www.googleapis.com/auth/gmail.modify",
    "gmail_send": "https://www.googleapis.com/auth/gmail.send",
}

# Receives (build_url, state): choose a redirect URI, call build_url with it,
# obtain the grant, and return (code, returned_state, redirect_uri).
GrantSource = Callable[[Callable[[str], str], str], tuple[str, str, str]]

_LOOPBACK_REDIRECT = "http://127.0.0.1:{port}"


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorization_url(
    *, client_id: str, redirect_uri: str, scope: str, state: str, code_verifier: str
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": _code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"


def _exchange_code(
    http: httpx.Client,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> str:
    response = http.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    if not 200 <= response.status_code < 300:
        raise ValueError(f"the token exchange failed with HTTP {response.status_code}")
    payload = response.json()
    refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError(
            "the token exchange returned no refresh token; the OAuth client may not "
            "be an installed application, or consent was not re-prompted"
        )
    return refresh_token


def _write_credential_file(directory: Path, server_id: str, document: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{server_id}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, document.encode("ascii"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return path


def run_bootstrap(
    *,
    client_id: str,
    client_secret: str,
    output_directory: Path,
    http: httpx.Client,
    grant_source: GrantSource,
    printer: Callable[[str], None] = print,
) -> int:
    for server_id, scope in SERVER_GOOGLE_SCOPES.items():
        state = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        chosen: dict[str, str] = {}

        def build_url(
            redirect_uri: str,
            *,
            _scope: str = scope,
            _state: str = state,
            _verifier: str = code_verifier,
            _chosen: dict[str, str] = chosen,
        ) -> str:
            _chosen["redirect_uri"] = redirect_uri
            return authorization_url(
                client_id=client_id,
                redirect_uri=redirect_uri,
                scope=_scope,
                state=_state,
                code_verifier=_verifier,
            )

        code, returned_state, redirect_uri = grant_source(build_url, state)
        if chosen.get("redirect_uri") != redirect_uri:
            raise ValueError(f"the consent for {server_id} changed its redirect URI")
        if returned_state != state:
            raise ValueError(f"the consent for {server_id} returned a mismatched state")
        refresh_token = _exchange_code(
            http,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        document = json.dumps(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "scope": scope,
            },
            separators=(",", ":"),
        )
        path = _write_credential_file(output_directory, server_id, document)
        printer(f"{server_id}: wrote {path} for scope {scope}")
    printer(
        "Set GMAIL_READ_CREDENTIAL_FILE, GMAIL_WRITE_CREDENTIAL_FILE, and "
        "GMAIL_SEND_CREDENTIAL_FILE to these paths and AGENT_EMAIL_ENABLED=1."
    )
    return 0


class _RedirectHandler(BaseHTTPRequestHandler):
    """Receives one loopback redirect and stores its code and state."""

    received: dict[str, str]

    def do_GET(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        type(self).received = {
            "code": query.get("code", [""])[0],
            "state": query.get("state", [""])[0],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Consent received. You can close this tab.")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def loopback_grant_source(printer: Callable[[str], None] = print) -> GrantSource:
    """Serve one redirect on an ephemeral loopback port per consent."""

    def grant(build_url: Callable[[str], str], state: str) -> tuple[str, str, str]:
        del state
        handler: type[_RedirectHandler] = type("_OneShot", (_RedirectHandler,), {"received": {}})
        with HTTPServer(("127.0.0.1", 0), handler) as server:
            port = server.server_address[1]
            redirect_uri = _LOOPBACK_REDIRECT.format(port=port)
            url = build_url(redirect_uri)
            thread = Thread(target=server.handle_request, daemon=True)
            thread.start()
            printer(f"Open this URL, sign in, and approve access:\n{url}")
            webbrowser.open(url)
            thread.join()
        received = handler.received
        return received.get("code", ""), received.get("state", ""), redirect_uri

    return grant
