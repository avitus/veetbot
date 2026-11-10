"""Best-effort live phase publishing, isolated from tool execution and persistence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from uuid import UUID

from agent_core.domain.browser_diagnostics import BrowserProgress
from agent_core.ports.live_events import LiveEventBroadcaster


@asynccontextmanager
async def publish_browser_progress(
    broadcaster: LiveEventBroadcaster | None,
    *,
    session_id: UUID,
    run_id: UUID,
    call_id: str,
    name: str,
) -> AsyncIterator[Callable[[BrowserProgress], None] | None]:
    if broadcaster is None:
        yield None
        return
    queue: asyncio.Queue[BrowserProgress] = asyncio.Queue(maxsize=1)

    def offer(update: BrowserProgress) -> None:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(update)

    async def publish() -> None:
        while True:
            update = await queue.get()
            try:
                async with asyncio.timeout(0.25):
                    await broadcaster.publish(
                        session_id,
                        run_id,
                        "tool.call.progress",
                        {
                            "run_id": str(run_id),
                            "call_id": call_id,
                            "name": name,
                            "browser_progress": update.model_dump(mode="json"),
                        },
                    )
            except Exception:
                # Best-effort presentation must not alter the result or retry policy.
                pass

    task = asyncio.create_task(publish())
    try:
        yield offer
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
