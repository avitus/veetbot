"""Plaintext proxy requests are individually host-scoped and audited."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.execution import proxy


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


async def test_plaintext_proxy_closes_after_one_audited_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        del host, port
        return ("192.0.2.10",)

    async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, Any]:
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
