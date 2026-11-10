"""Optional bounded browser phase stream over the authenticated session response."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from agent_core.domain.browser import BrowserProviderError
from agent_core.domain.browser_diagnostics import (
    PROGRESS_MEDIA_TYPE,
    BrowserProgress,
    browser_phase,
    collect_browser_diagnostics,
)
from agent_core.domain.errors import ConflictError


class _BrowserProgressResponse(StreamingResponse):
    """Close a stream suspended at a send as well as one waiting on a phase."""

    def __init__(self, frames: AsyncGenerator[bytes, None]) -> None:
        """Configure an unbuffered stream with explicit generator ownership."""
        super().__init__(
            frames,
            media_type=PROGRESS_MEDIA_TYPE,
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
        self._frames = frames

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Join operation cleanup even when disconnection interrupts an ASGI send."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette cancellation repeats at awaits, including generator cleanup.
            with anyio.CancelScope(shield=True):
                await self._frames.aclose()


async def diagnosed_operation[T](
    request: Request,
    operation: Awaitable[T],
    render: Callable[[T], dict[str, Any]],
    on_progress: Callable[[BrowserProgress], None] | None = None,
) -> dict[str, Any]:
    """Attach terminal diagnostics on success and preserve them on failure."""
    with collect_browser_diagnostics(
        clock=asyncio.get_running_loop().time, on_progress=on_progress
    ) as collector:
        try:
            with browser_phase("operation", "hosted"):
                result = await operation
            payload = render(result)
        finally:
            request.state.browser_diagnostics = collector.snapshot()
    payload["browser_diagnostics"] = collector.snapshot().model_dump(mode="json")
    return payload


def progress_response[T](
    request: Request, operation: Awaitable[T], render: Callable[[T], dict[str, Any]]
) -> StreamingResponse:
    """Stream coalesced phase updates followed by the original operation result."""

    async def frames() -> AsyncGenerator[bytes, None]:
        """Own the browser task and join it when the response closes."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)

        def offer(frame: dict[str, Any]) -> None:
            """Keep only the newest frame, allowing the terminal result to replace progress."""
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

        async def execute() -> None:
            """Run once and translate failures into content-free terminal envelopes."""
            status = 200
            try:
                body = await diagnosed_operation(
                    request,
                    operation,
                    render,
                    lambda update: offer(
                        {"type": "progress", "progress": update.model_dump(mode="json")}
                    ),
                )
            except BrowserProviderError as error:
                status = 409
                body = {
                    "error": {"code": error.reason_code, "message": "browser operation rejected"}
                }
            except asyncio.CancelledError:
                status = 503
                body = {
                    "error": {
                        "code": "tool.browser.provider_unavailable",
                        "message": "operation interrupted",
                    }
                }
            except ConflictError:
                status = 409
                body = {"error": {"code": "conflict", "message": "profile lifecycle conflict"}}
            except Exception:
                status = 500
                body = {"error": {"code": "internal_error", "message": "service unavailable"}}
            body["browser_diagnostics"] = request.state.browser_diagnostics.model_dump(mode="json")
            offer({"type": "result", "status": status, "body": body})

        task = asyncio.create_task(execute())
        try:
            while True:
                frame = await queue.get()
                yield (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                if frame["type"] == "result":
                    break
        finally:
            with anyio.CancelScope(shield=True):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    return _BrowserProgressResponse(frames())
