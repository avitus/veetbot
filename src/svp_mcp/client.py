"""Fixed-origin REST transport; provider failures never carry provider text."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any, Protocol

import anyio
import anyio.to_thread
import httpx

from svp_mcp.constants import (
    ANSWERED_STATUSES,
    DOCUMENT_URL,
    MAXIMUM_DOCUMENT_BYTES,
    MAXIMUM_PROBLEM_BYTES,
    MAXIMUM_PROBLEM_CHARACTERS,
    MAXIMUM_RESULT_BYTES,
)
from svp_mcp.errors import SvpError, SvpRejectionError
from svp_mcp.specification import is_confined, is_read_post


def _problem(raw: bytes) -> str:
    """Reduce a client-error body to the service's own bounded `detail` text."""

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        return ""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if detail is None:
        return ""
    text = detail if isinstance(detail, str) else json.dumps(detail)
    return text[:MAXIMUM_PROBLEM_CHARACTERS]


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
        self,
        method: str,
        url: str,
        query: Sequence[tuple[str, str]],
        body: Mapping[str, Any] | None,
        token: str,
        maximum_bytes: int,
    ) -> tuple[int, bytes]:
        try:
            async with self._http.stream(
                method,
                url,
                params=list(query) or None,
                json=None if body is None else dict(body),
                headers={"authorization": f"Bearer {token}", "accept": "application/json"},
                follow_redirects=False,
            ) as response:
                if response.status_code in ANSWERED_STATUSES:
                    explained = bytearray()
                    async for chunk in response.aiter_bytes():
                        explained.extend(chunk[: MAXIMUM_PROBLEM_BYTES - len(explained)])
                        if len(explained) >= MAXIMUM_PROBLEM_BYTES:
                            break
                    return response.status_code, bytes(explained)
                if not 200 <= response.status_code < 300:
                    return response.status_code, b""
                received = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(received) + len(chunk) > maximum_bytes:
                        raise SvpError("svp.response_too_large")
                    received.extend(chunk)
                return response.status_code, bytes(received)
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
        return await self._read("GET", url, query, None, maximum_bytes)

    async def post(
        self,
        url: str,
        body: Mapping[str, Any],
        query: Sequence[tuple[str, str]] = (),
        *,
        maximum_bytes: int = MAXIMUM_RESULT_BYTES,
    ) -> object:
        """Run one reviewed read `POST`; any other target is refused before a request."""

        if not is_read_post(url):
            raise SvpError("svp.arguments_invalid")
        return await self._read("POST", url, query, body, maximum_bytes)

    async def _read(
        self,
        method: str,
        url: str,
        query: Sequence[tuple[str, str]],
        body: Mapping[str, Any] | None,
        maximum_bytes: int,
    ) -> object:
        token = await self._token(None)
        status, received = await self._send(method, url, query, body, token, maximum_bytes)
        if status == 401:
            # A refused token means the request was not acted on, so one retry is safe.
            token = await self._token(token)
            status, received = await self._send(method, url, query, body, token, maximum_bytes)
        if status in ANSWERED_STATUSES:
            raise SvpRejectionError(status, _problem(received))
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
            return json.loads(received)
        except (ValueError, UnicodeError):
            raise SvpError("svp.response_invalid") from None

    async def document(self) -> dict[str, Any]:
        """Fetch the published OpenAPI document once for this server's lifetime."""

        async with self._document_lock:
            if self._document is None:
                try:
                    # The document's own location is fixed and is not a callable target.
                    payload = await self._read(
                        "GET", DOCUMENT_URL, (), None, MAXIMUM_DOCUMENT_BYTES
                    )
                except SvpRejectionError:
                    raise SvpError("svp.specification_invalid") from None
                except SvpError as exc:
                    if exc.code in {"svp.response_too_large", "svp.response_invalid"}:
                        raise SvpError("svp.specification_invalid") from None
                    raise
                if not isinstance(payload, dict):
                    raise SvpError("svp.specification_invalid")
                self._document = payload
            return self._document
