"""Bounded notification worker loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from agent_core.ports.determinism import Clock

type DispatchNotifications = Callable[[], Awaitable[int]]
type WaitForNotificationWakeup = Callable[[float], Awaitable[None]]
logger = logging.getLogger(__name__)


class NotificationWorker:
    def __init__(
        self,
        *,
        dispatch_once: DispatchNotifications,
        clock: Clock,
        fallback_poll_seconds: float,
        wait_for_wakeup: WaitForNotificationWakeup | None = None,
    ) -> None:
        if fallback_poll_seconds <= 0:
            raise ValueError("notification fallback poll must be positive")
        self._dispatch_once = dispatch_once
        self._clock = clock
        self._fallback_poll = fallback_poll_seconds
        self._wait_for_wakeup = wait_for_wakeup
        self._stopping = False
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()

    async def run_once(self) -> int:
        return await self._dispatch_once()

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("notification dispatcher scan failed")
            if self._stopping:
                break
            awaitable = (
                self._clock.sleep(self._fallback_poll)
                if self._wait_for_wakeup is None
                else self._wait_for_wakeup(self._fallback_poll)
            )
            try:
                await self._wait_or_stop(awaitable)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("notification wakeup failed; using poll fallback")
                await self._wait_or_stop(self._clock.sleep(self._fallback_poll))

    async def _wait_or_stop(self, awaitable: Awaitable[None]) -> None:
        waiting = asyncio.ensure_future(awaitable)
        stopping = asyncio.create_task(self._stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {waiting, stopping},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiting in done:
                await waiting
        finally:
            for task in (waiting, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiting, stopping, return_exceptions=True)
