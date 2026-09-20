"""HTTP implementation of the one-click unsubscribe transport port."""

from __future__ import annotations

import asyncio
import ssl

import httpx

from agent_core.domain.unsubscribe import (
    ONE_CLICK_BODY,
    ONE_CLICK_CONTENT_TYPE,
    UNSUBSCRIBE_MAX_RESPONSE_BYTES,
    UNSUBSCRIBE_REQUEST_DEADLINE_SECONDS,
    UNSUBSCRIBE_USER_AGENT,
    UnsubscribeOutcomeCode,
)
from agent_core.domain.web import is_public_https_url

_HEADERS = {
    "Content-Type": ONE_CLICK_CONTENT_TYPE,
    "User-Agent": UNSUBSCRIBE_USER_AGENT,
}
# httpcore reports a non-2xx answer to its CONNECT as "<status> <reason phrase>".
_DESTINATION_REFUSED_STATUS = 403
_CERTIFICATE_FAILURE = "certificate verify failed"


class HttpOneClickTransport:
    """Send RFC 8058's constant request through the proxy and keep only its status class."""

    def __init__(
        self,
        proxy_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        deadline_seconds: float = UNSUBSCRIBE_REQUEST_DEADLINE_SECONDS,
    ) -> None:
        self._deadline_seconds = deadline_seconds
        self._timeout = httpx.Timeout(deadline_seconds).as_dict()
        # One transport rather than a client: no cookie jar, no redirect machinery,
        # no ambient environment, and no request log naming the destination whose
        # path carries the recipient token. TLS verification keeps httpx's default
        # trust store and has no override here.
        self._transport = transport or httpx.AsyncHTTPTransport(
            proxy=proxy_url,
            trust_env=False,
        )

    async def post(self, url: str) -> UnsubscribeOutcomeCode:
        """Dial one checked public HTTPS destination; every failure is a closed code."""
        if not is_public_https_url(url):
            return UnsubscribeOutcomeCode.DESTINATION_REFUSED
        try:
            # Every byte is a constant: nothing a caller, an owner, or a model
            # authored reaches the request, and no stored cookie or credential
            # joins it. A header-derived address the client refuses to parse
            # never becomes a dial.
            request = httpx.Request(
                "POST",
                url,
                headers=_HEADERS,
                content=ONE_CLICK_BODY,
                extensions={"timeout": self._timeout},
            )
            async with asyncio.timeout(self._deadline_seconds):
                response = await self._transport.handle_async_request(request)
                try:
                    await _discard(response)
                finally:
                    await response.aclose()
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            return _failure_code(exc)
        return _status_code(response.status_code)

    async def close(self) -> None:
        await self._transport.aclose()


async def _discard(response: httpx.Response) -> None:
    """Read a bounded prefix of the raw body: a compressed response is never inflated."""
    if response.is_stream_consumed:
        return
    read = 0
    async for chunk in response.aiter_raw():
        read += len(chunk)
        if read >= UNSUBSCRIBE_MAX_RESPONSE_BYTES:
            return


def _status_code(status: int) -> UnsubscribeOutcomeCode:
    if 200 <= status < 300:
        return UnsubscribeOutcomeCode.ACCEPTED
    if 300 <= status < 400:
        return UnsubscribeOutcomeCode.REDIRECT_REFUSED
    return UnsubscribeOutcomeCode.REJECTED


def _failure_code(failure: BaseException) -> UnsubscribeOutcomeCode:
    """Name the failure without naming the destination; an unknown one is unreachable."""
    if isinstance(failure, httpx.InvalidURL):
        return UnsubscribeOutcomeCode.DESTINATION_REFUSED
    if isinstance(failure, httpx.ProxyError):
        refused = _connect_status(failure) == _DESTINATION_REFUSED_STATUS
        return (
            UnsubscribeOutcomeCode.DESTINATION_REFUSED
            if refused
            else UnsubscribeOutcomeCode.UNREACHABLE
        )
    if _is_tls_failure(failure):
        return UnsubscribeOutcomeCode.TLS_FAILED
    return UnsubscribeOutcomeCode.UNREACHABLE


def _connect_status(failure: httpx.ProxyError) -> int | None:
    leading = str(failure).split(" ", 1)[0]
    return int(leading) if leading.isdigit() else None


def _is_tls_failure(failure: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = failure
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return isinstance(failure, httpx.ConnectError) and _CERTIFICATE_FAILURE in str(failure).lower()
