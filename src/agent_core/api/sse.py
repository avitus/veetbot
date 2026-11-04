"""SSE framing where only durable sequences receive ids."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from agent_core.domain.views import PersistedStreamFrame, StreamFrame


def encode_sse(frame: StreamFrame) -> bytes:
    lines: list[str] = []
    if isinstance(frame, PersistedStreamFrame):
        lines.append(f"id: {frame.sequence}")
    lines.append(f"event: {frame.event}")
    lines.append(
        "data: "
        + json.dumps(
            frame.data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def heartbeat() -> bytes:
    return b": heartbeat\n\n"


class EventStreamResponse(StreamingResponse):
    """An SSE response whose stream is closed when the request ends, however it ends.

    Starlette abandons the body iterator when a client drops, and only garbage
    collection would reach it after that. A stream can own a database connection,
    so its cleanup runs here, shielded from the request's cancelled scope.
    """

    def __init__(
        self,
        frames: AsyncGenerator[bytes, None],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            frames,
            media_type="text/event-stream",
            headers={**(headers or {}), "X-Accel-Buffering": "no"},
        )
        self._frames = frames

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._frames.aclose()
