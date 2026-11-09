"""One-time installed-app OAuth consent ceremony."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode

import httpx

from calendar_mcp.client import SCOPES, CalendarError, parse_credential

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
LOOPBACK_REDIRECT_HOST = "127.0.0.1"
UPSTREAM_MAXIMUM_BYTES = 1024 * 1024
GOOGLE_SCOPES = {"read": SCOPES}


class BootstrapError(RuntimeError):
    """A content-free consent-ceremony failure."""


def _challenge(verifier: str) -> str:
    """Derive the unpadded S256 PKCE challenge for a verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _authorization_url(
    *,
    client_id: str,
    scope: str,
    redirect_uri: str,
    state: str,
    verifier: str,
) -> str:
    """Build Google consent parameters with offline access, state and PKCE."""
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{
        urlencode(
            {
                'client_id': client_id,
                'redirect_uri': redirect_uri,
                'response_type': 'code',
                'scope': scope,
                'access_type': 'offline',
                'prompt': 'consent',
                'state': state,
                'code_challenge': _challenge(verifier),
                'code_challenge_method': 'S256',
            }
        )
    }"


async def bootstrap_credentials(
    *,
    client_id: str,
    client_secret: str,
    calendar_ids: list[str],
    output_directory: Path,
    authorize: Callable[[str, str], str],
    http_client: httpx.AsyncClient | None = None,
) -> tuple[Path, ...]:
    """Consent once per mode and publish one owner-only credential document."""

    if not client_id or not client_secret:
        raise BootstrapError("OAuth client configuration is incomplete")
    try:
        parse_credential(
            json.dumps(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": "pending",
                    "scope": SCOPES,
                    "calendar_ids": calendar_ids,
                }
            )
        )
    except CalendarError as exc:
        raise BootstrapError("calendar selection is invalid") from exc
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not output_directory.is_dir() or output_directory.is_symlink():
        raise BootstrapError("credential output directory is invalid")
    paths = tuple(output_directory / f"calendar-{mode}.json" for mode in GOOGLE_SCOPES)
    if any(path.exists() or path.is_symlink() for path in paths):
        raise BootstrapError("credential output already exists")

    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
    )
    owns_client = http_client is None
    documents: list[dict[str, object]] = []
    try:
        for index, (mode, scope) in enumerate(GOOGLE_SCOPES.items()):
            verifier = secrets.token_urlsafe(48)
            state = secrets.token_urlsafe(32)
            redirect_uri = f"http://{LOOPBACK_REDIRECT_HOST}:{8765 + index}"
            authorization_url = _authorization_url(
                client_id=client_id,
                scope=scope,
                redirect_uri=redirect_uri,
                state=state,
                verifier=verifier,
            )
            code = authorize(mode, authorization_url)
            if not isinstance(code, str) or not code or len(code) > 4096:
                raise BootstrapError("authorization response was invalid")
            try:
                async with (
                    asyncio.timeout(30),
                    client.stream(
                        "POST",
                        GOOGLE_TOKEN_ENDPOINT,
                        follow_redirects=False,
                        data={
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "code": code,
                            "code_verifier": verifier,
                            "grant_type": "authorization_code",
                            "redirect_uri": redirect_uri,
                        },
                    ) as response,
                ):
                    if response.status_code != 200:
                        raise BootstrapError("token exchange was rejected")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > UPSTREAM_MAXIMUM_BYTES:
                            raise BootstrapError("token exchange returned invalid output")
            except (httpx.HTTPError, TimeoutError) as exc:
                raise BootstrapError("token exchange was unavailable") from exc
            try:
                payload: object = json.loads(body)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise BootstrapError("token exchange returned invalid output") from exc
            if not isinstance(payload, dict):
                raise BootstrapError("token exchange returned invalid output")
            refresh_token = payload.get("refresh_token")
            granted_scope = payload.get("scope")
            if (
                not isinstance(refresh_token, str)
                or not refresh_token
                or len(refresh_token) > 4096
                or not isinstance(granted_scope, str)
                or set(granted_scope.split()) != set(scope.split())
            ):
                raise BootstrapError("token exchange omitted the exact grant")
            documents.append(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "scope": scope,
                    "calendar_ids": calendar_ids,
                }
            )
    finally:
        if owns_client:
            await client.aclose()

    created: list[Path] = []
    try:
        for path, document in zip(paths, documents, strict=True):
            encoded = json.dumps(document).encode("utf-8")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            try:
                stream = os.fdopen(descriptor, "wb", closefd=True)
            except BaseException:
                with suppress(OSError):
                    os.close(descriptor)
                raise
            with stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise

    for (mode, scope), path in zip(GOOGLE_SCOPES.items(), paths, strict=True):
        print(f"{mode}: {path} ({scope})")
    return paths
