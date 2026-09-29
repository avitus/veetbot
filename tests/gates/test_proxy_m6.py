"""Plaintext proxy requests are individually host-scoped and audited."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_core.domain.execution import EgressDestination, EgressMode, EgressPolicy
from agent_core.execution import proxy


async def test_browser_transport_is_enabled_only_by_trusted_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser resource transport is selected explicitly, never by worker policy."""
    policies: list[tuple[Any, str]] = []
    sentinel = object()

    async def start(policy: Any, *, tenant_id: str) -> Any:
        policies.append((policy, tenant_id))
        return sentinel

    monkeypatch.setattr(proxy, "_start_proxy", start)
    navigation = EgressPolicy(
        EgressMode.ALLOWLIST,
        (EgressDestination("site.example", frozenset({443})),),
    )
    assert await proxy.start_browser_egress_proxy(navigation, tenant_id="tenant-a") is sentinel
    assert policies == [(("browser_https", ()), "tenant-a")]
    policies.clear()
    await proxy.start_worker_egress_proxy(navigation, tenant_id="tenant-a")
    assert policies == [(("allowlist", (("site.example", frozenset({443})),)), "tenant-a")]
    with pytest.raises(ValueError):
        await proxy.start_browser_egress_proxy(EgressPolicy(), tenant_id="tenant-a")


async def test_browser_transport_refuses_plaintext_before_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTPS resource redirect cannot downgrade to plaintext, even on port 443."""
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"GET http://cdn.other.example:443/app.js HTTP/1.1\r\nHost: cdn.other.example:443\r\n\r\n"
    )
    reader.feed_eof()
    writer = _Writer()

    async def resolved(_host: str, _port: int) -> tuple[str, ...]:
        raise AssertionError("plaintext request reached DNS")

    monkeypatch.setattr(proxy, "_resolved", resolved)
    await proxy._handle(reader, writer, ("browser_https", ()))  # type: ignore[arg-type]
    assert bytes(writer.data).startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize(
    ("target", "addresses", "allowed"),
    [
        ("cdn.other.example:443", ("93.184.216.34",), True),
        ("cdn.other.example:443", ("127.0.0.1",), False),
        ("cdn.other.example:443", ("93.184.216.34", "10.0.0.1"), False),
        ("cdn.other.example:443", ("169.254.169.254",), False),
        ("cdn.other.example:443", ("::ffff:127.0.0.1",), False),
        ("cdn.other.example:443", (), False),
        ("cdn.other.example:80", ("93.184.216.34",), False),
        ("127.0.0.1:443", ("93.184.216.34",), False),
        ("localhost:443", ("93.184.216.34",), False),
        ("metadata.internal:443", ("93.184.216.34",), False),
    ],
)
async def test_browser_resource_proxy_checks_public_addresses_before_dial(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    addresses: tuple[str, ...],
    allowed: bool,
) -> None:
    """The browser-only transport admits CDNs but pins every dial to public DNS."""
    reader = asyncio.StreamReader()
    reader.feed_data(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    reader.feed_eof()
    writer = _Writer()
    dials: list[tuple[str, int]] = []

    async def resolved(_host: str, _port: int) -> tuple[str, ...]:
        return addresses

    async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, Any]:
        dials.append((host, port))
        upstream = asyncio.StreamReader()
        upstream.feed_eof()
        return upstream, _Writer()

    monkeypatch.setattr(proxy, "_resolved", resolved)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    await proxy._handle(reader, writer, ("browser_https", ()))  # type: ignore[arg-type]

    assert bool(dials) is allowed
    if allowed:
        assert dials == [(addresses[0], 443)]
        assert bytes(writer.data).startswith(b"HTTP/1.1 200")
    else:
        assert bytes(writer.data).startswith(b"HTTP/1.1 403")


def test_sandbox_configuration_cannot_select_browser_resource_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only trusted browser composition may enable public-resource transport."""
    monkeypatch.setenv("AGENT_EGRESS_POLICY", '{"mode":"browser_https","destinations":[]}')
    with pytest.raises(ValueError):
        proxy._policy()


@pytest.mark.parametrize(
    "serialized",
    [
        '{"mode":"allow","destinations":[]}',
        '{"mode":"unsubscribe_https","destinations":[]}',
        '["allowlist"]',
        '{"mode":"allowlist","destinations":{"host":"pypi.org","ports":[443]}}',
        '{"mode":"allowlist","destinations":[{"host":"10.0.0.1","ports":[443]}]}',
        '{"mode":"allowlist","destinations":[{"host":"*.*.example.com","ports":[443]}]}',
        '{"mode":"allowlist","destinations":[{"host":"pypi.org","ports":[]}]}',
    ],
    ids=[
        "open_mode",
        "unsubscribe_transport",
        "not_a_mapping",
        "destinations_not_a_list",
        "address_destination",
        "two_label_wildcard",
        "no_ports",
    ],
)
def test_serialized_sandbox_policy_is_validated_at_proxy_start(
    monkeypatch: pytest.MonkeyPatch, serialized: str
) -> None:
    """The proxy re-validates the grammar it is handed rather than trusting its caller."""
    monkeypatch.setenv("AGENT_EGRESS_POLICY", serialized)
    with pytest.raises(ValueError):
        proxy._policy()


def test_serialized_sandbox_policy_defaults_to_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_EGRESS_POLICY", "{}")
    assert proxy._policy() == ("deny", ())
    monkeypatch.setenv(
        "AGENT_EGRESS_POLICY",
        '{"mode":"allowlist","destinations":[{"host":"*.pythonhosted.org","ports":[443]}]}',
    )
    assert proxy._policy() == ("allowlist", (("*.pythonhosted.org", frozenset({443})),))


async def test_deny_mode_refuses_and_audits_a_connect_without_dialing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
    reader.feed_eof()
    writer = _Writer()

    async def resolved(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async def open_connection(_host: str, _port: int) -> tuple[asyncio.StreamReader, Any]:
        raise AssertionError("deny mode reached the dial boundary")

    monkeypatch.setattr(proxy, "_resolved", resolved)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    await proxy._handle(reader, writer, ("deny", ()), tenant_id="tenant-a", run_id="run-1")  # type: ignore[arg-type]

    assert bytes(writer.data).startswith(b"HTTP/1.1 403 Forbidden")
    audit = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert audit == {
        "host": "pypi.org",
        "port": 443,
        "reason": "mode_deny",
        "resolved_addresses": ["93.184.216.34"],
        "run_id": "run-1",
        "tenant_id": "tenant-a",
    }


class _Writer:
    def __init__(self) -> None:
        """Capture proxy output and connection closure without creating a socket."""
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        """Append the bytes the proxy sends to this fake connection."""
        self.data.extend(data)

    async def drain(self) -> None:
        """Complete the fake output flush without network I/O."""
        return None

    def close(self) -> None:
        """Record that the proxy closed this connection."""
        self.closed = True

    def is_closing(self) -> bool:
        """Expose the connection state used by proxy error cleanup."""
        return self.closed


async def test_plaintext_proxy_closes_after_one_audited_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward only one allowed HTTP request and close before a pipelined disallowed request."""
    client_reader = asyncio.StreamReader()
    client_reader.feed_data(
        b"GET http://allowed.example:8080/first HTTP/1.1\r\n"
        b"Host: allowed.example:8080\r\n\r\n"
        b"GET http://denied.example:8080/second HTTP/1.1\r\n"
        b"Host: denied.example:8080\r\n\r\n"
    )
    client_reader.feed_eof()
    client_writer = _Writer()
    upstream_reader = asyncio.StreamReader()
    upstream_reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
    upstream_reader.feed_eof()
    upstream_writer = _Writer()
    audited: list[tuple[str, int]] = []

    async def resolved(host: str, port: int) -> tuple[str, ...]:
        """Supply a deterministic address for the allowed plaintext proxy request."""
        del host, port
        return ("192.0.2.10",)

    async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, Any]:
        """Return the scripted upstream while preserving the proxy dial boundary."""
        del host, port
        return upstream_reader, upstream_writer

    monkeypatch.setattr(proxy, "_resolved", resolved)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(proxy, "evaluate_core", lambda *_args: (True, "allowed"))
    monkeypatch.setattr(
        proxy,
        "_log",
        lambda host, port, _addresses, _reason: audited.append((host, port)),
    )

    await proxy._handle(
        client_reader,
        client_writer,  # type: ignore[arg-type]
        ("allowlist", (("allowed.example", frozenset({8080})),)),
    )

    forwarded = bytes(upstream_writer.data)
    assert b"/first" in forwarded
    assert b"Connection: close" in forwarded
    assert b"denied.example" not in forwarded
    assert audited == [("allowed.example", 8080)]
    assert bytes(client_writer.data).endswith(b"OK")
    assert client_writer.closed is True


@pytest.mark.parametrize(
    "raw_request",
    [
        (b"GET https://allowed.example:443/ HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n"),
        (
            b"POST http://allowed.example:8080/ HTTP/1.1\r\n"
            b"Host: allowed.example:8080\r\n"
            b"Content-Length: 1\r\nContent-Length: 1\r\n\r\nx"
        ),
        (
            b"POST http://allowed.example:8080/ HTTP/1.1\r\n"
            b"Host: allowed.example:8080\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\n\r\n"
        ),
        (
            b"POST http://allowed.example:8080/ HTTP/1.1\r\n"
            b"Host: allowed.example:8080\r\n"
            b"Content-Length: -1\r\n\r\n"
        ),
        # The Host header must name the request target, exactly once.
        (b"GET http://allowed.example:8080/ HTTP/1.1\r\nHost: denied.example:8080\r\n\r\n"),
        (b"GET http://allowed.example:8080/ HTTP/1.1\r\nHost: allowed.example:80\r\n\r\n"),
        (b"GET http://allowed.example:8080/ HTTP/1.1\r\n\r\n"),
        (
            b"GET http://allowed.example:8080/ HTTP/1.1\r\n"
            b"Host: allowed.example:8080\r\nHost: allowed.example:8080\r\n\r\n"
        ),
        (b"GET http://allowed.example:8080/ HTTP/1.1\r\nHost allowed.example\r\n\r\n"),
        # Targets carry an explicit port; the grammar has no default.
        (b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\n\r\n"),
        (b"GET /relative HTTP/1.1\r\nHost: allowed.example:8080\r\n\r\n"),
        (b"CONNECT allowed.example HTTP/1.1\r\nHost: allowed.example\r\n\r\n"),
    ],
    ids=[
        "https_absolute_uri",
        "repeated_content_length",
        "transfer_encoding",
        "negative_content_length",
        "host_mismatch",
        "host_port_mismatch",
        "missing_host",
        "repeated_host",
        "malformed_header",
        "implicit_port",
        "relative_target",
        "connect_without_port",
    ],
)
async def test_plaintext_proxy_rejects_ambiguous_requests_before_dialing(
    monkeypatch: pytest.MonkeyPatch, raw_request: bytes
) -> None:
    """Reject unsupported or ambiguous HTTP framing before contacting an upstream."""
    reader = asyncio.StreamReader()
    reader.feed_data(raw_request)
    reader.feed_eof()
    writer = _Writer()
    dialed = False

    async def open_connection(_host: str, _port: int) -> tuple[asyncio.StreamReader, Any]:
        """Record and reject an upstream dial attempted for invalid request framing."""
        nonlocal dialed
        dialed = True
        raise AssertionError("invalid proxy request reached the dial boundary")

    async def resolved(_host: str, _port: int) -> tuple[str, ...]:
        raise AssertionError("invalid proxy request reached DNS")

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(proxy, "_resolved", resolved)

    await proxy._handle(
        reader,
        writer,  # type: ignore[arg-type]
        ("allowlist", (("allowed.example", frozenset({443, 8080})),)),
    )

    assert dialed is False
    assert bytes(writer.data).startswith(b"HTTP/1.1 502 Bad Gateway")
    assert writer.closed is True


async def test_https_redirect_connect_is_refused_before_upstream_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTPS redirect to an unlisted www host cannot open a proxy tunnel."""
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"CONNECT www.allowed.example:443 HTTP/1.1\r\nHost: www.allowed.example:443\r\n\r\n"
    )
    reader.feed_eof()
    writer = _Writer()
    dialed = False

    async def resolved(host: str, port: int) -> tuple[str, ...]:
        """Supply a public address so refusal depends on the exact hostname policy."""
        assert (host, port) == ("www.allowed.example", 443)
        return ("93.184.216.34",)

    async def open_connection(_host: str, _port: int) -> tuple[asyncio.StreamReader, Any]:
        """Record any attempt to tunnel to the disallowed redirect target."""
        nonlocal dialed
        dialed = True
        raise AssertionError("refused HTTPS redirect reached the upstream dial boundary")

    monkeypatch.setattr(proxy, "_resolved", resolved)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    await proxy._handle(
        reader,
        writer,  # type: ignore[arg-type]
        ("allowlist", (("allowed.example", frozenset({443})),)),
    )
    assert bytes(writer.data).startswith(b"HTTP/1.1 403 Forbidden")
    assert dialed is False
    assert writer.closed is True
