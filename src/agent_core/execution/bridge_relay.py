"""In-sandbox Unix-socket relay for the worker-owned tool bridge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

_MAX_REQUEST_BYTES = 64 * 1024


async def _run() -> None:
    socket_path = Path(os.environ["AGENT_TOOL_BRIDGE_SOCKET"])
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    loop = asyncio.get_running_loop()
    responses = asyncio.StreamReader(limit=_MAX_REQUEST_BYTES + 1)
    protocol = asyncio.StreamReaderProtocol(responses)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    stdout_transport, stdout_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), sys.stdout.buffer
    )
    response_requests = asyncio.StreamWriter(
        stdout_transport, cast(Any, stdout_protocol), None, loop
    )
    try:
        raw_token = await responses.readline()
    except ValueError as exc:
        raise RuntimeError("bridge bootstrap token exceeds its bound") from exc
    if not raw_token or len(raw_token) > _MAX_REQUEST_BYTES:
        raise RuntimeError("bridge bootstrap token is missing or oversized")
    token = raw_token.rstrip(b"\n").decode("utf-8")
    response_lock = asyncio.Lock()
    response_overrun = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                fatal_response = False
                try:
                    request = await reader.readline()
                except ValueError:
                    writer.write(_denied("bridge.request_too_large") + b"\n")
                    await writer.drain()
                    return
                if not request:
                    return
                if len(request) > _MAX_REQUEST_BYTES:
                    response = _denied("bridge.request_too_large")
                else:
                    try:
                        payload = json.loads(request)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = None
                    if not isinstance(payload, dict):
                        response = _denied("bridge.protocol_error")
                    else:
                        payload["token"] = token
                        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                        if len(encoded) > _MAX_REQUEST_BYTES:
                            response = _denied("bridge.request_too_large")
                        else:
                            async with response_lock:
                                response_requests.write(encoded + b"\n")
                                await response_requests.drain()
                                try:
                                    response = await responses.readline()
                                except ValueError:
                                    response = _denied("bridge.response_too_large")
                                    fatal_response = True
                                if not response:
                                    response_overrun.set()
                                    return
                writer.write(response.rstrip(b"\n") + b"\n")
                await writer.drain()
                if fatal_response:
                    response_overrun.set()
                    return
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=socket_path, limit=_MAX_REQUEST_BYTES + 1)
    os.chmod(socket_path, 0o600)
    async with server:
        try:
            await response_overrun.wait()
        finally:
            response_requests.close()


def _denied(reason_code: str) -> bytes:
    return json.dumps(
        {"status": "denied", "reason_code": reason_code, "retryable": False},
        separators=(",", ":"),
    ).encode("utf-8")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
