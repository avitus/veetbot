"""Minimal audited HTTP CONNECT proxy for allowlisted sandbox egress."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import suppress
from urllib.parse import urlsplit

from agent_core.execution.egress_core import evaluate_core, validate_host_and_ports

_MAX_HEADER_BYTES = 64 * 1024


def _policy() -> tuple[str, tuple[tuple[str, frozenset[int]], ...]]:
    loaded: object = json.loads(os.environ["AGENT_EGRESS_POLICY"])
    if not isinstance(loaded, dict):
        raise ValueError("egress policy must be a mapping")
    destinations = loaded.get("destinations", [])
    if not isinstance(destinations, list):
        raise ValueError("egress destinations must be a list")
    parsed = tuple(
        (str(item["host"]), frozenset(int(port) for port in item["ports"]))
        for item in destinations
        if isinstance(item, dict)
    )
    for host, ports in parsed:
        validate_host_and_ports(host, ports)
    return str(loaded.get("mode", "deny")), parsed


async def _resolved(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(record[4][0]) for record in records))


def _log(host: str, port: int, addresses: tuple[str, ...], reason: str) -> None:
    record = {
        "tenant_id": os.environ.get("AGENT_TENANT_ID", ""),
        "run_id": os.environ.get("AGENT_RUN_ID", ""),
        "host": host,
        "port": port,
        "resolved_addresses": addresses,
        "reason": reason,
    }
    print(json.dumps(record, separators=(",", ":"), sort_keys=True), flush=True)


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def _relay_exact(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, remaining: int
) -> None:
    while remaining:
        chunk = await reader.read(min(64 * 1024, remaining))
        if not chunk:
            raise asyncio.IncompleteReadError(partial=b"", expected=remaining)
        remaining -= len(chunk)
        writer.write(chunk)
        await writer.drain()


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    policy: tuple[str, tuple[tuple[str, frozenset[int]], ...]],
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        header = await reader.readuntil(b"\r\n\r\n")
        if len(header) > _MAX_HEADER_BYTES:
            raise ValueError("proxy header too large")
        first, *rest = header.split(b"\r\n")
        method, raw_target, version = first.decode("ascii").split(" ", 2)
        if method.upper() == "CONNECT":
            raw_host, host_separator, raw_port = raw_target.rpartition(":")
            if not host_separator:
                raise ValueError("CONNECT target requires an explicit port")
            host, port = raw_host.strip("[]"), int(raw_port)
            upstream_header = None
            content_length = 0
        else:
            target = urlsplit(raw_target)
            if target.scheme.lower() != "http":
                raise ValueError("plaintext proxy requests require an http absolute URI")
            if not target.hostname or target.port is None:
                raise ValueError("absolute proxy URI requires an explicit port")
            host, port = target.hostname, target.port
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            header_lines: list[bytes] = []
            content_length = 0
            content_length_seen = False
            host_header: tuple[str, int] | None = None
            for line in rest:
                if not line:
                    continue
                header_name, header_separator, value = line.partition(b":")
                if not header_separator:
                    raise ValueError("malformed proxy header")
                lowered = header_name.strip().lower()
                if lowered == b"transfer-encoding":
                    raise ValueError("transfer-encoded proxy requests are unsupported")
                if lowered == b"content-length":
                    raw_length = value.strip()
                    if content_length_seen or not raw_length or not raw_length.isdigit():
                        raise ValueError("invalid or repeated proxy content length")
                    content_length_seen = True
                    content_length = int(raw_length)
                if lowered == b"host":
                    if host_header is not None:
                        raise ValueError("multiple proxy host headers")
                    parsed_host = urlsplit("//" + value.strip().decode("ascii"))
                    if parsed_host.hostname is None:
                        raise ValueError("invalid proxy host header")
                    default_port = 443 if target.scheme.lower() == "https" else 80
                    host_header = (parsed_host.hostname, parsed_host.port or default_port)
                if lowered not in {b"connection", b"proxy-connection"}:
                    header_lines.append(line)
            if host_header != (host, port):
                raise ValueError("proxy host header does not match the request target")
            header_lines.append(b"Connection: close")
            upstream_header = b" ".join((method.encode(), path.encode(), version.encode()))
            upstream_header += b"\r\n" + b"".join(line + b"\r\n" for line in header_lines)
            upstream_header += b"\r\n"
        addresses = await _resolved(host, port)
        allowed, reason = evaluate_core(policy[0], policy[1], host, port, addresses)
        _log(host, port, addresses, reason)
        if not allowed:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        # Dial the already-checked address; do not resolve the name again.
        upstream_reader, upstream_writer = await asyncio.open_connection(addresses[0], port)
        if method.upper() == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        elif upstream_header is not None:
            upstream_writer.write(upstream_header)
        await writer.drain()
        await upstream_writer.drain()
        if method.upper() == "CONNECT":
            await asyncio.gather(
                _relay(reader, upstream_writer),
                _relay(upstream_reader, writer),
            )
        else:
            await _relay_exact(reader, upstream_writer, content_length)
            await _relay(upstream_reader, writer)
    except (
        ValueError,
        UnicodeError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        OSError,
    ) as exc:
        print(
            json.dumps({"reason": "proxy_error", "error_class": type(exc).__name__}),
            file=sys.stderr,
            flush=True,
        )
        if not writer.is_closing():
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            with suppress(OSError):
                await writer.drain()
    finally:
        writer.close()
        if upstream_writer is not None:
            upstream_writer.close()


async def main() -> None:
    policy = _policy()
    bind_host = os.environ.get("AGENT_PROXY_BIND_HOST", "127.0.0.1")
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, policy),
        bind_host,
        3128,
        limit=_MAX_HEADER_BYTES + 1,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
