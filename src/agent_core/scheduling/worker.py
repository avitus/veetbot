"""Bounded schedule scanning without execution capabilities."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import UUID

from agent_core.domain.schedules import ScheduleOccurrence
from agent_core.observability.schedules import ScheduleMetrics
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import ScheduleUnitOfWorkFactory

type MaterializeSchedule = Callable[[UUID], Awaitable[ScheduleOccurrence | None]]
type WaitForScheduleWakeup = Callable[[float], Awaitable[None]]
logger = logging.getLogger(__name__)


class ScheduleWorker:
    def __init__(
        self,
        *,
        uow_factory: ScheduleUnitOfWorkFactory,
        materialize: MaterializeSchedule,
        clock: Clock,
        scan_batch: int,
        fallback_poll_seconds: float,
        admission_backoff_seconds: float,
        wait_for_wakeup: WaitForScheduleWakeup | None = None,
        metrics: ScheduleMetrics | None = None,
    ) -> None:
        if scan_batch <= 0:
            raise ValueError("schedule scan batch must be positive")
        if fallback_poll_seconds <= 0:
            raise ValueError("schedule fallback poll must be positive")
        if admission_backoff_seconds <= 0:
            raise ValueError("schedule admission backoff must be positive")
        self._uow_factory = uow_factory
        self._materialize = materialize
        self._clock = clock
        self._scan_batch = scan_batch
        self._fallback_poll = fallback_poll_seconds
        self._admission_backoff = admission_backoff_seconds
        self._wait_for_wakeup = wait_for_wakeup
        self._metrics = metrics or ScheduleMetrics()
        self._stopping = False
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()

    async def _wait_or_stop(self, awaitable: Awaitable[None]) -> bool:
        waiting: asyncio.Future[None] = asyncio.ensure_future(awaitable)
        stopping = asyncio.create_task(self._wait_for_stop())
        try:
            done, _pending = await asyncio.wait(
                {waiting, stopping},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopping in done:
                if waiting in done:
                    await waiting
                return True
            await waiting
            return False
        finally:
            for task in (waiting, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiting, stopping, return_exceptions=True)

    async def _wait_for_stop(self) -> None:
        await self._stop_event.wait()

    async def run_once(self) -> int:
        started = perf_counter()
        async with self._uow_factory() as uow:
            due = await uow.schedules.due(self._clock.now(), self._scan_batch)
        self._metrics.record_scan(due_count=len(due), duration_seconds=perf_counter() - started)
        completed = 0
        for schedule_id in due:
            try:
                await self._materialize(schedule_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "schedule materialization failed",
                    extra={"schedule_id": str(schedule_id)},
                )
            else:
                completed += 1
        return completed

    async def wait_seconds(self) -> float:
        async with self._uow_factory() as uow:
            next_fire_at = await uow.schedules.next_fire_at()
        if next_fire_at is None:
            return self._fallback_poll
        until_due = (next_fire_at - self._clock.now()).total_seconds()
        if until_due <= 0:
            return min(self._admission_backoff, self._fallback_poll)
        return min(until_due, self._fallback_poll)

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("schedule worker scan failed")
            if self._stopping:
                break
            wait_seconds = await self.wait_seconds()
            if self._wait_for_wakeup is None:
                await self._wait_or_stop(self._clock.sleep(wait_seconds))
                continue
            try:
                await self._wait_or_stop(self._wait_for_wakeup(wait_seconds))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("schedule worker wakeup failed; using poll fallback")
                await self._wait_or_stop(self._clock.sleep(wait_seconds))
