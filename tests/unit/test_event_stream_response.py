"""An event stream is closed when its request ends, however the request ends.

Starlette abandons the body iterator when a client drops, leaving it suspended
until garbage collection happens to reach it. A stream that owns a database
connection cannot wait for that (2026-11-03 outage).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from starlette.types import Message

from agent_core.api.sse import EventStreamResponse


async def _serve_until_client_drops(response: EventStreamResponse) -> None:
    client_left = asyncio.Event()
    request_sent = False

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await client_left.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            client_left.set()
            await asyncio.sleep(1)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}
    await asyncio.wait_for(response(scope, receive, send), timeout=5)


async def test_a_stream_abandoned_mid_send_finishes_its_cleanup_before_the_request_ends() -> None:
    steps: list[str] = []

    async def frames() -> AsyncGenerator[bytes, None]:
        try:
            yield b": first\n\n"
            yield b": never sent\n\n"
        finally:
            steps.append("cleanup started")
            await asyncio.sleep(0)  # An awaiting cleanup must survive the cancelled scope.
            steps.append("cleanup finished")

    # The test keeps the generator alive, so only an explicit close can reach its cleanup.
    stream = frames()
    await _serve_until_client_drops(EventStreamResponse(stream))

    assert steps == ["cleanup started", "cleanup finished"]


async def test_a_stream_that_ends_by_itself_is_delivered_and_closed() -> None:
    sent: list[bytes] = []
    closed = False

    async def frames() -> AsyncGenerator[bytes, None]:
        nonlocal closed
        try:
            yield b": only\n\n"
        finally:
            closed = True

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            sent.append(message["body"])

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}
    await EventStreamResponse(frames())(scope, receive, send)

    assert sent == [b": only\n\n", b""]
    assert closed


def test_the_response_declares_an_event_stream_and_disables_proxy_buffering() -> None:
    async def frames() -> AsyncGenerator[bytes, None]:
        yield b""

    stream = frames()
    response = EventStreamResponse(stream, headers={"Cache-Control": "no-cache"})

    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
