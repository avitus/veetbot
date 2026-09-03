"""One-time installed-app OAuth consent ceremony."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode

import httpx

from gmail_mcp.client import GmailCredential
from gmail_mcp.constants import (
    GOOGLE_AUTHORIZATION_ENDPOINT,
    GOOGLE_SCOPES,
    GOOGLE_TOKEN_ENDPOINT,
    LOOPBACK_REDIRECT_HOST,
    UPSTREAM_MAXIMUM_BYTES,
)


class BootstrapError(RuntimeError):
    """A content-free consent-ceremony failure."""


_ACCOUNT_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _challenge(verifier: str) -> str:
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
    account_id: str | None = None,
    output_directory: Path,
    authorize: Callable[[str, str], str],
    http_client: httpx.AsyncClient | None = None,
) -> tuple[Path, ...]:
    """Consent once per mode and publish three owner-only credential documents."""

    if not client_id or not client_secret:
        raise BootstrapError("OAuth client configuration is incomplete")
    if account_id is not None and _ACCOUNT_ID.fullmatch(account_id) is None:
        raise BootstrapError("account id is invalid")
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not output_directory.is_dir() or output_directory.is_symlink():
        raise BootstrapError("credential output directory is invalid")
    paths = tuple(output_directory / f"gmail-{mode}.json" for mode in GOOGLE_SCOPES)
    if any(path.exists() or path.is_symlink() for path in paths):
        raise BootstrapError("credential output already exists")

    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
    )
    owns_client = http_client is None
    documents: list[GmailCredential] = []
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
                response = await client.post(
                    GOOGLE_TOKEN_ENDPOINT,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": code,
                        "code_verifier": verifier,
                        "grant_type": "authorization_code",
                        "redirect_uri": redirect_uri,
                    },
                )
            except httpx.HTTPError as exc:
                raise BootstrapError("token exchange was unavailable") from exc
            if response.status_code != 200 or len(response.content) > UPSTREAM_MAXIMUM_BYTES:
                raise BootstrapError("token exchange was rejected")
            try:
                payload: object = response.json()
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise BootstrapError("token exchange returned invalid output") from exc
            if not isinstance(payload, dict):
                raise BootstrapError("token exchange returned invalid output")
            refresh_token = payload.get("refresh_token")
            granted_scope = payload.get("scope")
            if not isinstance(refresh_token, str) or not refresh_token or granted_scope != scope:
                raise BootstrapError("token exchange omitted the exact grant")
            documents.append(
                GmailCredential(
                    client_id=client_id,
                    client_secret=client_secret,
                    refresh_token=refresh_token,
                    scope=scope,
                    account_id=account_id,
                )
            )
    finally:
        if owns_client:
            await client.aclose()

    created: list[Path] = []
    try:
        for path, document in zip(paths, documents, strict=True):
            encoded = document.as_json().encode("utf-8")
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
