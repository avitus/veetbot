"""Exactly-once, single-process dispatcher used by Milestone 1."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID


class InlineRunDispatcher:
    def __init__(
        self,
        execute: Callable[[UUID], Awaitable[None]],
        unit_of_work_open: Callable[[], bool],
        *,
        delay_before: Callable[[UUID], Awaitable[float | None]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        tasks: set[asyncio.Task[None]] | None = None,
    ) -> None:
        """Run queued work in process; `delay_before` honors a run parked for later.

        `delay_before` returns the seconds a queued run must still wait, or None once
        it is no longer queued (taken back by Undo, ADR-0279). A parked run waits out
        its window in a background task, so the request that parked it returns at
        once and Undo can reach it, just as with a worker's claim; `tasks` lets the
        composition retire those background tasks at shutdown.
        """
        self._execute = execute
        self._unit_of_work_open = unit_of_work_open
        self._delay_before = delay_before
        self._sleep = sleep
        self._tasks = tasks if tasks is not None else set()
        self._dispatched: set[UUID] = set()
        self._lock = asyncio.Lock()

    async def dispatch(self, run_id: UUID) -> None:
        if self._unit_of_work_open():
            raise RuntimeError("inline dispatch must happen after the creating commit")
        async with self._lock:
            if run_id in self._dispatched:
                return
            self._dispatched.add(run_id)
        if self._delay_before is not None:
            delay = await self._delay_before(run_id)
            if delay is None:
                return
            if delay > 0 and self._sleep is not None:
                task = asyncio.create_task(self._run_later(run_id, delay))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return
        await self._run_now(run_id)

    async def _run_later(self, run_id: UUID, delay: float) -> None:
        """Wait out the window, then run unless the run was taken back meanwhile."""
        assert self._sleep is not None and self._delay_before is not None
        await self._sleep(delay)
        if await self._delay_before(run_id) is None:
            return
        await self._run_now(run_id)

    async def drain(self) -> None:
        """Wait for every parked run to settle; tests and shutdown call this."""
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_now(self, run_id: UUID) -> None:
        try:
            await self._execute(run_id)
        except BaseException:
            # Exactly-once holds for successful execution; a failed execution
            # must stay dispatchable or the run is unrecoverable in-process.
            async with self._lock:
                self._dispatched.discard(run_id)
            raise

    async def resume(self, run_id: UUID) -> None:
        async with self._lock:
            self._dispatched.discard(run_id)
        await self.dispatch(run_id)
