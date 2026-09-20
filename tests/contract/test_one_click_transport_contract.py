"""Shared behavioral contract for every one-click unsubscribe transport."""

from __future__ import annotations

import socket
import ssl
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from agent_core.adapters.unsubscribe.http import HttpOneClickTransport
from agent_core.domain.unsubscribe import UnsubscribeOutcomeCode
from agent_core.ports.unsubscribe import OneClickTransport

TransportFactory = Callable[[httpx.AsyncBaseTransport], OneClickTransport]

PROXY_URL = "http://127.0.0.1:65000"
DESTINATION = "https://unsubscribe.sender.example/u/recipient-token?list=7"


def transport_factories() -> tuple[tuple[str, TransportFactory], ...]:
    return (("http", lambda wire: HttpOneClickTransport(PROXY_URL, transport=wire)),)


async def _body() -> AsyncIterator[bytes]:
    """Stream one small response body the transport is required to discard."""
    yield b"<html>sender copy the transport never parses</html>"


def _handshake_failure() -> httpx.ConnectError:
    """Reproduce the chain httpx builds when the TLS handshake fails."""
    mapped = ConnectionError("handshake failed")
    mapped.__context__ = ssl.SSLCertVerificationError("certificate verify failed")
    failure = httpx.ConnectError("handshake failed")
    failure.__cause__ = mapped
    return failure


@pytest.mark.parametrize(("transport_name", "factory"), transport_factories())
@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, UnsubscribeOutcomeCode.ACCEPTED),
        (202, UnsubscribeOutcomeCode.ACCEPTED),
        (302, UnsubscribeOutcomeCode.REDIRECT_REFUSED),
        (404, UnsubscribeOutcomeCode.REJECTED),
        (503, UnsubscribeOutcomeCode.REJECTED),
    ],
)
async def test_one_click_transport_reports_only_the_status_class(
    transport_name: str,
    factory: TransportFactory,
    status: int,
    outcome: UnsubscribeOutcomeCode,
) -> None:
    """Each observable outcome is a member of the closed code set, and nothing else."""
    del transport_name

    async def wire(request: httpx.Request) -> httpx.Response:
        """Answer with the status the case selected and a body to discard."""
        del request
        return httpx.Response(status, content=_body())

    transport = factory(httpx.MockTransport(wire))
    try:
        observed = await transport.post(DESTINATION)
    finally:
        await transport.close()

    assert observed is outcome
    assert observed in set(UnsubscribeOutcomeCode)


@pytest.mark.parametrize(("transport_name", "factory"), transport_factories())
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ProxyError("403 Forbidden"),
        httpx.ProxyError("502 Bad Gateway"),
        _handshake_failure(),
        httpx.ConnectTimeout("timed out"),
        httpx.RemoteProtocolError("disconnected"),
        socket.gaierror("name or service not known"),
    ],
)
async def test_one_click_transport_never_raises_for_a_remote_failure(
    transport_name: str,
    factory: TransportFactory,
    failure: Exception,
) -> None:
    """A remote or network failure is a closed code, never an exception at the port."""
    del transport_name

    async def wire(request: httpx.Request) -> httpx.Response:
        """Fail the way the installed client surfaces this case."""
        del request
        raise failure

    transport = factory(httpx.MockTransport(wire))
    try:
        observed = await transport.post(DESTINATION)
    finally:
        await transport.close()

    assert observed in set(UnsubscribeOutcomeCode)
    assert observed is not UnsubscribeOutcomeCode.ACCEPTED


@pytest.mark.parametrize(("transport_name", "factory"), transport_factories())
async def test_one_click_transport_refuses_a_destination_that_is_not_public_https(
    transport_name: str,
    factory: TransportFactory,
) -> None:
    """A destination failing the public-HTTPS rule is refused before any network activity."""
    del transport_name

    async def wire(request: httpx.Request) -> httpx.Response:
        """Fail the test if a refused destination reaches the network."""
        raise AssertionError(f"refused destination dialled: {request.method}")

    transport = factory(httpx.MockTransport(wire))
    try:
        for refused in (
            "http://sender.example/u",
            "https://127.0.0.1/u",
            "https://localhost/u",
            "https://sender.example/u\r\nX-Injected: 1",
        ):
            assert await transport.post(refused) is UnsubscribeOutcomeCode.DESTINATION_REFUSED
    finally:
        await transport.close()


@pytest.mark.parametrize(("transport_name", "factory"), transport_factories())
async def test_one_click_transport_close_is_idempotent(
    transport_name: str,
    factory: TransportFactory,
) -> None:
    """Closing twice is safe, so recovery may close a transport it did not open."""
    del transport_name

    async def wire(request: httpx.Request) -> httpx.Response:
        """Answer one accepted request before the transport closes."""
        del request
        return httpx.Response(200, content=_body())

    transport = factory(httpx.MockTransport(wire))
    assert await transport.post(DESTINATION) is UnsubscribeOutcomeCode.ACCEPTED
    await transport.close()
    await transport.close()
