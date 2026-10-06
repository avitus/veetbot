"""Authorization-code grant with PKCE against fixed endpoints; failures carry no upstream text."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from svp_mcp.constants import (
    AUTHORIZATION_ENDPOINT,
    CLIENT_NAME,
    DEFAULT_EXPIRY_SECONDS,
    LOOPBACK_REDIRECT_URI,
    MAXIMUM_TOKEN_BYTES,
    REGISTRATION_ENDPOINT,
    RESOURCE,
    SCOPES,
    TOKEN_ENDPOINT,
)
from svp_mcp.errors import SvpError

_SCOPE = " ".join(SCOPES)


@dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class Grant:
    access_token: str
    refresh_token: str | None
    expires_in: int


def _opaque(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 8192
        and value.isascii()
        and value.isprintable()
        and " " not in value
    )


def pkce() -> tuple[str, str]:
    """Return a fresh verifier and its S256 challenge."""

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorization_url(client_id: str, *, state: str, challenge: str) -> str:
    return f"{AUTHORIZATION_ENDPOINT}?{
        urlencode(
            {
                'response_type': 'code',
                'client_id': client_id,
                'redirect_uri': LOOPBACK_REDIRECT_URI,
                'scope': _SCOPE,
                'state': state,
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
                'resource': RESOURCE,
            }
        )
    }"


def _post(http: httpx.Client, url: str, *, grant: bool, **body: Any) -> dict[str, Any]:
    try:
        with http.stream("POST", url, follow_redirects=False, **body) as response:
            status = response.status_code
            if not 200 <= status < 300:
                raise SvpError(
                    "svp.rate_limited"
                    if status == 429
                    else "svp.provider_unavailable"
                    if status >= 500
                    else "svp.credential_rejected"
                    if grant and status in {400, 401, 403}
                    else "svp.provider_rejected"
                )
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > MAXIMUM_TOKEN_BYTES:
                    raise SvpError("svp.response_invalid")
                raw.extend(chunk)
    except httpx.HTTPError:
        raise SvpError("svp.provider_unavailable") from None
    try:
        payload: object = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SvpError("svp.response_invalid") from None
    if not isinstance(payload, dict):
        raise SvpError("svp.response_invalid")
    return payload


def register(http: httpx.Client) -> Client:
    """Register one confidential client bound to the fixed loopback redirect."""

    payload = _post(
        http,
        REGISTRATION_ENDPOINT,
        grant=False,
        json={
            "client_name": CLIENT_NAME,
            "redirect_uris": [LOOPBACK_REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": _SCOPE,
        },
    )
    client_id, client_secret = payload.get("client_id"), payload.get("client_secret")
    if not _opaque(client_id) or not _opaque(client_secret):
        raise SvpError("svp.response_invalid")
    return Client(str(client_id), str(client_secret))


def _grant(http: httpx.Client, client: Client, form: dict[str, str]) -> Grant:
    payload = _post(
        http,
        TOKEN_ENDPOINT,
        grant=True,
        data={
            **form,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "resource": RESOURCE,
        },
    )
    access_token, token_type = payload.get("access_token"), payload.get("token_type")
    expires_in = payload.get("expires_in", DEFAULT_EXPIRY_SECONDS)
    refresh_token = payload.get("refresh_token")
    if (
        not _opaque(access_token)
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int | float)
        or not 0 < expires_in <= 10 * 365 * 86400
        or (refresh_token is not None and not _opaque(refresh_token))
    ):
        raise SvpError("svp.response_invalid")
    return Grant(
        str(access_token),
        None if refresh_token is None else str(refresh_token),
        int(expires_in),
    )


def exchange(http: httpx.Client, client: Client, *, code: str, verifier: str) -> Grant:
    return _grant(
        http,
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": LOOPBACK_REDIRECT_URI,
        },
    )


def refresh(http: httpx.Client, client: Client, refresh_token: str) -> Grant:
    return _grant(http, client, {"grant_type": "refresh_token", "refresh_token": refresh_token})
