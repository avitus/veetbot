"""The broker-delivered credential document and the refresh exchange.

The platform resolves one ``credential_ref`` per server and places the value
in this process's environment as one declared variable. The value is a JSON
document only this package understands; the refresh exchange against Google
runs here, checking expiry at use, and no token material ever leaves the
process — errors carry stable codes and parse failures carry no content.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from gmail_mcp.errors import (
    CREDENTIAL_REJECTED,
    RATE_LIMITED,
    REJECTED,
    UNAVAILABLE,
    GmailServerError,
)

CREDENTIAL_VARIABLE = "GMAIL_MCP_CREDENTIAL"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_EXPIRY_MARGIN_SECONDS = 60.0


@dataclass(frozen=True)
class GmailCredential:
    """The parsed credential document: identity, secret, grant, and scope."""

    client_id: str
    client_secret: str
    refresh_token: str
    scope: str

    @classmethod
    def parse(cls, raw: str) -> GmailCredential:
        try:
            document = json.loads(raw)
        except ValueError:
            raise ValueError("the credential document is not JSON") from None
        if not isinstance(document, dict):
            raise ValueError("the credential document is not an object")
        fields = {}
        for name in ("client_id", "client_secret", "refresh_token", "scope"):
            value = document.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"the credential document is missing {name}")
            fields[name] = value
        return cls(**fields)


class RefreshingTokenSource:
    """Exchange the refresh token at use, caching until near expiry."""

    def __init__(
        self,
        credential: GmailCredential,
        *,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credential = credential
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    async def access_token(self) -> str:
        if self._token is not None and self._clock() < self._expires_at:
            return self._token
        try:
            response = await self._http.post(
                TOKEN_ENDPOINT,
                data={
                    "client_id": self._credential.client_id,
                    "client_secret": self._credential.client_secret,
                    "refresh_token": self._credential.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError:
            raise GmailServerError(UNAVAILABLE) from None
        if 300 <= response.status_code < 400:
            # A redirect is refused, never followed: the client secret and
            # refresh token go to the fixed token endpoint or nowhere.
            raise GmailServerError(REJECTED)
        if response.status_code in {400, 401, 403}:
            raise GmailServerError(CREDENTIAL_REJECTED)
        if response.status_code == 429:
            raise GmailServerError(RATE_LIMITED)
        if response.status_code != 200:
            raise GmailServerError(UNAVAILABLE)
        try:
            payload = response.json()
        except ValueError:
            raise GmailServerError(UNAVAILABLE) from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        lifetime = payload.get("expires_in") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GmailServerError(CREDENTIAL_REJECTED)
        self._token = token
        seconds = float(lifetime) if isinstance(lifetime, int | float) else 0.0
        self._expires_at = self._clock() + max(seconds - _EXPIRY_MARGIN_SECONDS, 0.0)
        return token
