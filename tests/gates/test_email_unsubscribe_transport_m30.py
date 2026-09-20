"""Milestone 30 transport gates: one fixed request over a public-HTTPS-only transport."""

from __future__ import annotations

import asyncio
import inspect
import logging
import socket
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_core.adapters.unsubscribe.http import HttpOneClickTransport
from agent_core.domain.execution import EgressDestination, EgressMode, EgressPolicy
from agent_core.domain.unsubscribe import (
    ONE_CLICK_BODY,
    ONE_CLICK_CONTENT_TYPE,
    UNSUBSCRIBE_MAX_RESPONSE_BYTES,
    UNSUBSCRIBE_USER_AGENT,
    UnsubscribeOutcomeCode,
)
from agent_core.execution import proxy
from tests.gates.test_proxy_m6 import _Writer

PROXY_URL = "http://127.0.0.1:65000"
DESTINATION = "https://unsubscribe.sender.example/u/recipient-token?list=7"
CREDENTIAL_HEADERS = frozenset({"cookie", "authorization", "referer", "origin"})
UNSUBSCRIBE_POLICY: tuple[str, tuple[tuple[str, frozenset[int]], ...]] = ("unsubscribe_https", ())
BROWSER_POLICY: tuple[str, tuple[tuple[str, frozenset[int]], ...]] = ("browser_https", ())
PUBLIC_ADDRESS = "93.184.216.34"
type Decision = tuple[str, int, tuple[str, ...], str]


class _Body:
    """A response body that reports how many bytes a reader actually pulled from it."""

    def __init__(self, *, chunk: int, chunks: int) -> None:
        self.chunk = chunk
        self.chunks = chunks
        self.produced = 0

    async def __call__(self) -> AsyncIterator[bytes]:
        """Yield the scripted chunks, counting every byte the transport consumes."""
        for _ in range(self.chunks):
            self.produced += self.chunk
            yield b"\x00" * self.chunk


async def _never_answers(request: httpx.Request) -> httpx.Response:
    """Accept the request and never produce a response."""
    del request
    await asyncio.sleep(60)
    raise AssertionError("the request deadline did not expire")


async def _endless_body() -> AsyncIterator[bytes]:
    """Trickle a body that never reaches the read bound."""
    while True:
        await asyncio.sleep(0.01)
        yield b"\x00"


async def _never_finishes(request: httpx.Request) -> httpx.Response:
    """Answer immediately with a body that never ends."""
    del request
    return httpx.Response(200, content=_endless_body())


def _tls_failure(*, chained: bool) -> httpx.ConnectError:
    """Shape the chain a handshake failure really builds: ssl, then httpcore, then httpx."""
    if not chained:
        return httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed (_ssl.c:1006)"
        )
    mapped = ConnectionError("handshake failed")
    mapped.__context__ = ssl.SSLCertVerificationError("certificate verify failed")
    failure = httpx.ConnectError("handshake failed")
    failure.__cause__ = mapped
    return failure


def _last(audited: list[Decision]) -> Decision:
    """Return the decision the proxy audited most recently."""
    return audited[-1]


def _reader(request: bytes) -> asyncio.StreamReader:
    """Feed one complete proxy request and close the client side."""
    reader = asyncio.StreamReader()
    reader.feed_data(request)
    reader.feed_eof()
    return reader


def _connect(target: str) -> bytes:
    """Build the CONNECT request a TLS client sends for one destination."""
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()


async def test_unsubscribe_request_is_fixed(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hard gate 4: one constant credential-free POST, bounded, deadlined, and content-free."""
    sent: list[httpx.Request] = []
    body = _Body(chunk=4096, chunks=64)
    answer = [200]

    async def wire(request: httpx.Request) -> httpx.Response:
        """Record the request and answer with the status the current case selected."""
        sent.append(request)
        return httpx.Response(
            answer[0],
            headers={
                "Set-Cookie": "session=sender-tracker; Path=/",
                # A declared encoding the raw reader never inflates.
                "Content-Encoding": "gzip",
                "Location": "https://elsewhere.example/confirm",
            },
            content=body(),
        )

    transport = HttpOneClickTransport(PROXY_URL, transport=httpx.MockTransport(wire))
    try:
        for refused in (
            "http://sender.example/u",
            f"https://{PUBLIC_ADDRESS}/u",
            "https://localhost/u",
            "https://sender.example:8443/u",
            "https://" + "owner:userinfo" + "@sender.example/u",
            "https://sender.internal/u",
            # A split-stripping URL parser accepts this; the client must not dial it.
            "https://sender.example/u\r\nX-Injected: 1",
        ):
            assert await transport.post(refused) is UnsubscribeOutcomeCode.DESTINATION_REFUSED
        assert sent == []

        with caplog.at_level(logging.DEBUG):
            assert await transport.post(DESTINATION) is UnsubscribeOutcomeCode.ACCEPTED
            assert await transport.post(DESTINATION) is UnsubscribeOutcomeCode.ACCEPTED

        assert body.produced == 2 * UNSUBSCRIBE_MAX_RESPONSE_BYTES
        assert len(sent) == 2
        assert ONE_CLICK_BODY == b"List-Unsubscribe=One-Click"
        for request in sent:
            assert request.method == "POST"
            assert str(request.url) == DESTINATION
            assert request.content == ONE_CLICK_BODY
            assert request.headers["content-type"] == ONE_CLICK_CONTENT_TYPE
            assert request.headers["user-agent"] == UNSUBSCRIBE_USER_AGENT
            assert request.headers["content-length"] == str(len(ONE_CLICK_BODY)) == "26"
            assert not CREDENTIAL_HEADERS & set(request.headers)

        for status, outcome in (
            (204, UnsubscribeOutcomeCode.ACCEPTED),
            (299, UnsubscribeOutcomeCode.ACCEPTED),
            (301, UnsubscribeOutcomeCode.REDIRECT_REFUSED),
            (302, UnsubscribeOutcomeCode.REDIRECT_REFUSED),
            (307, UnsubscribeOutcomeCode.REDIRECT_REFUSED),
            (400, UnsubscribeOutcomeCode.REJECTED),
            (404, UnsubscribeOutcomeCode.REJECTED),
            (429, UnsubscribeOutcomeCode.REJECTED),
            (500, UnsubscribeOutcomeCode.REJECTED),
        ):
            answer[0] = status
            dispatched = len(sent)
            assert await transport.post(DESTINATION) is outcome
            assert len(sent) == dispatched + 1

        source = Path(inspect.getsourcefile(HttpOneClickTransport) or "").read_text(
            encoding="utf-8"
        )
        assert "verify" not in inspect.signature(HttpOneClickTransport.__init__).parameters
        assert "verify=" not in source
        assert "check_hostname" not in source
        assert "CERT_NONE" not in source

        assert caplog.records == []
        captured = capsys.readouterr()
        assert "sender.example" not in captured.out + captured.err
        assert "recipient-token" not in captured.out + captured.err
    finally:
        await transport.close()

    for handler in (_never_answers, _never_finishes):
        slow = HttpOneClickTransport(
            PROXY_URL,
            transport=httpx.MockTransport(handler),
            deadline_seconds=0.05,
        )
        try:
            assert await slow.post(DESTINATION) is UnsubscribeOutcomeCode.UNREACHABLE
        finally:
            await slow.close()


async def test_unsubscribe_transport_maps_remote_failures_to_closed_codes() -> None:
    """A refused CONNECT, a handshake failure, and a dead host each close honestly."""
    failures: list[Exception] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        """Fail the way the installed httpx surfaces the current case."""
        del request
        raise failures[-1]

    transport = HttpOneClickTransport(PROXY_URL, transport=httpx.MockTransport(wire))
    try:
        for failure, outcome in (
            # httpcore reports a non-2xx CONNECT answer as "<status> <reason>".
            (httpx.ProxyError("403 Forbidden"), UnsubscribeOutcomeCode.DESTINATION_REFUSED),
            (httpx.ProxyError("502 Bad Gateway"), UnsubscribeOutcomeCode.UNREACHABLE),
            (httpx.ProxyError(""), UnsubscribeOutcomeCode.UNREACHABLE),
            (_tls_failure(chained=True), UnsubscribeOutcomeCode.TLS_FAILED),
            (_tls_failure(chained=False), UnsubscribeOutcomeCode.TLS_FAILED),
            (httpx.ConnectError("no route"), UnsubscribeOutcomeCode.UNREACHABLE),
            (httpx.ReadTimeout("timed out"), UnsubscribeOutcomeCode.UNREACHABLE),
            (httpx.RemoteProtocolError("disconnected"), UnsubscribeOutcomeCode.UNREACHABLE),
            (socket.gaierror("name or service not known"), UnsubscribeOutcomeCode.UNREACHABLE),
        ):
            failures.append(failure)
            assert await transport.post(DESTINATION) is outcome
    finally:
        await transport.close()


async def test_unsubscribe_transport_dials_only_through_its_proxy() -> None:
    """Without an injected transport every request leaves through the process-local proxy."""
    transport = HttpOneClickTransport(PROXY_URL)
    try:
        pool: Any = getattr(transport._transport, "_pool", None)
        assert type(pool).__name__ == "AsyncHTTPProxy"
        assert (pool._proxy_url.host, pool._proxy_url.port) == (b"127.0.0.1", 65000)
    finally:
        await transport.close()


async def test_unsubscribe_egress_is_public_https_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard gate 5: ADR-0098's public-HTTPS rule under its own name and refusal reason."""
    audited: list[Decision] = []
    monkeypatch.setattr(
        proxy,
        "_log",
        lambda host, port, addresses, reason: audited.append((host, port, addresses, reason)),
    )

    async def never_resolves(_host: str, _port: int) -> tuple[str, ...]:
        """Fail the test if a refusal that precedes DNS reaches the resolver."""
        raise AssertionError("a request refused by shape reached DNS")

    async def refuse_dial(_host: str, _port: int) -> tuple[asyncio.StreamReader, Any]:
        """Fail the test if a refused destination reaches the dial boundary."""
        raise AssertionError("a refused destination reached the dial boundary")

    monkeypatch.setattr(proxy, "_resolved", never_resolves)
    monkeypatch.setattr(asyncio, "open_connection", refuse_dial)

    plaintext = _Writer()
    await proxy._handle(
        _reader(b"GET http://sender.example:443/u HTTP/1.1\r\nHost: sender.example:443\r\n\r\n"),
        plaintext,  # type: ignore[arg-type]
        UNSUBSCRIBE_POLICY,
    )
    assert bytes(plaintext.data).startswith(b"HTTP/1.1 403")
    assert _last(audited)[3] == "unsubscribe_destination_denied"

    for target in (
        "sender.example:80",
        "sender.example:8443",
        f"{PUBLIC_ADDRESS}:443",
        "[::1]:443",
        "localhost:443",
        "intranet:443",
        "sender.internal:443",
        "sender.local:443",
        "sender.localhost:443",
        "sender.home:443",
        "sender.lan:443",
        "sender.123:443",
    ):
        writer = _Writer()
        await proxy._handle(
            _reader(_connect(target)),
            writer,  # type: ignore[arg-type]
            UNSUBSCRIBE_POLICY,
        )
        assert bytes(writer.data).startswith(b"HTTP/1.1 403"), target
        assert _last(audited)[3] == "unsubscribe_destination_denied", target

    addresses: tuple[str, ...] = ()

    async def resolves(_host: str, _port: int) -> tuple[str, ...]:
        """Return the addresses the current denylist case resolves to."""
        return addresses

    monkeypatch.setattr(proxy, "_resolved", resolves)
    for addresses in (
        ("10.0.0.7",),
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("172.16.0.5",),
        ("192.168.1.10",),
        ("100.64.0.1",),
        ("::1",),
        ("fc00::1",),
        ("fe80::1",),
        ("::ffff:169.254.169.254",),
        (PUBLIC_ADDRESS, "10.0.0.7"),
        (),
    ):
        writer = _Writer()
        await proxy._handle(
            _reader(_connect("sender.example:443")),
            writer,  # type: ignore[arg-type]
            UNSUBSCRIBE_POLICY,
        )
        assert bytes(writer.data).startswith(b"HTTP/1.1 403"), addresses
        assert _last(audited) == ("sender.example", 443, addresses, "private_address")

    dials: list[tuple[str, int]] = []

    async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, Any]:
        """Record the dialled address and complete the tunnel without a socket."""
        dials.append((host, port))
        upstream = asyncio.StreamReader()
        upstream.feed_eof()
        return upstream, _Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    addresses = (PUBLIC_ADDRESS, "93.184.216.35")
    allowed = _Writer()
    await proxy._handle(
        _reader(_connect("sender.example:443")),
        allowed,  # type: ignore[arg-type]
        UNSUBSCRIBE_POLICY,
    )
    assert bytes(allowed.data).startswith(b"HTTP/1.1 200")
    # The checked address is dialled, never the name.
    assert dials == [(PUBLIC_ADDRESS, 443)]
    assert _last(audited) == ("sender.example", 443, addresses, "allowed")

    monkeypatch.setenv("AGENT_EGRESS_POLICY", '{"mode":"unsubscribe_https","destinations":[]}')
    with pytest.raises(ValueError):
        proxy._policy()

    bound: list[tuple[Any, str]] = []
    sentinel = object()

    async def start(policy: Any, *, tenant_id: str) -> Any:
        """Capture the immutable transport policy the constructor binds."""
        bound.append((policy, tenant_id))
        return sentinel

    monkeypatch.setattr(proxy, "_start_proxy", start)
    assert await proxy.start_unsubscribe_egress_proxy(tenant_id="tenant-a") is sentinel
    assert bound == [(UNSUBSCRIBE_POLICY, "tenant-a")]
    # The operator allowlist cannot reach the transport: there is nowhere to pass one.
    assert list(inspect.signature(proxy.start_unsubscribe_egress_proxy).parameters) == ["tenant_id"]

    bound.clear()
    navigation = EgressPolicy(
        EgressMode.ALLOWLIST,
        (EgressDestination("site.example", frozenset({443})),),
    )
    assert await proxy.start_browser_egress_proxy(navigation, tenant_id="tenant-a") is sentinel
    assert await proxy.start_worker_egress_proxy(navigation, tenant_id="tenant-a") is sentinel
    assert bound == [
        (BROWSER_POLICY, "tenant-a"),
        (("allowlist", (("site.example", frozenset({443})),)), "tenant-a"),
    ]

    browser = _Writer()
    await proxy._handle(
        _reader(_connect("sender.example:80")),
        browser,  # type: ignore[arg-type]
        BROWSER_POLICY,
    )
    assert bytes(browser.data).startswith(b"HTTP/1.1 403")
    assert _last(audited)[3] == "browser_destination_denied"
