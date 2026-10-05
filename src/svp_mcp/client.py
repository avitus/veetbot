"""Fixed-origin REST transport; provider failures never carry provider text."""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import partial
from typing import Any, Protocol

import anyio
import anyio.to_thread
import httpx

from svp_mcp.constants import DOCUMENT_URL, MAXIMUM_DOCUMENT_BYTES, MAXIMUM_RESULT_BYTES
from svp_mcp.errors import SvpError
from svp_mcp.specification import is_confined


class TokenSource(Protocol):
    def access_token(self, *, rejected: str | None = None) -> str: ...


class SvpClient:
    def __init__(
        self, tokens: TokenSource, *, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self._tokens = tokens
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False
        )
        self._owns_http = http_client is None
        self._document: dict[str, Any] | None = None
        self._document_lock = anyio.Lock()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _token(self, rejected: str | None) -> str:
        # The grant holder takes a file lock and may refresh over the network.
        return await anyio.to_thread.run_sync(partial(self._tokens.access_token, rejected=rejected))

    async def _send(
        self, url: str, query: Sequence[tuple[str, str]], token: str, maximum_bytes: int
    ) -> tuple[int, bytes]:
        try:
            async with self._http.stream(
                "GET",
                url,
                params=list(query) or None,
                headers={"authorization": f"Bearer {token}", "accept": "application/json"},
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    return response.status_code, b""
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > maximum_bytes:
                        raise SvpError("svp.response_too_large")
                    body.extend(chunk)
                return response.status_code, bytes(body)
        except httpx.HTTPError:
            raise SvpError("svp.provider_unavailable") from None

    async def get(
        self,
        url: str,
        query: Sequence[tuple[str, str]] = (),
        *,
        maximum_bytes: int = MAXIMUM_RESULT_BYTES,
    ) -> object:
        """Read one confined URL as JSON, replacing a rejected token exactly once."""

        if not is_confined(url):
            raise SvpError("svp.arguments_invalid")
        return await self._read(url, query, maximum_bytes)

    async def _read(self, url: str, query: Sequence[tuple[str, str]], maximum_bytes: int) -> object:
        token = await self._token(None)
        status, body = await self._send(url, query, token, maximum_bytes)
        if status == 401:
            token = await self._token(token)
            status, body = await self._send(url, query, token, maximum_bytes)
        if not 200 <= status < 300:
            raise SvpError(
                "svp.credential_rejected"
                if status in {401, 403}
                else "svp.rate_limited"
                if status == 429
                else "svp.provider_unavailable"
                if status >= 500
                else "svp.provider_rejected"
            )
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise SvpError("svp.response_invalid") from None

    async def document(self) -> dict[str, Any]:
        """Fetch the published OpenAPI document once for this server's lifetime."""

        async with self._document_lock:
            if self._document is None:
                try:
                    # The document's own location is fixed and is not a callable target.
                    payload = await self._read(DOCUMENT_URL, (), MAXIMUM_DOCUMENT_BYTES)
                except SvpError as exc:
                    if exc.code in {"svp.response_too_large", "svp.response_invalid"}:
                        raise SvpError("svp.specification_invalid") from None
                    raise
                if not isinstance(payload, dict):
                    raise SvpError("svp.specification_invalid")
                self._document = payload
            return self._document
