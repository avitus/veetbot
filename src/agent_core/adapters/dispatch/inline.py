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
    ) -> None:
        self._execute = execute
        self._unit_of_work_open = unit_of_work_open
        self._dispatched: set[UUID] = set()
        self._lock = asyncio.Lock()

    async def dispatch(self, run_id: UUID) -> None:
        if self._unit_of_work_open():
            raise RuntimeError("inline dispatch must happen after the creating commit")
        async with self._lock:
            if run_id in self._dispatched:
                return
            self._dispatched.add(run_id)
        await self._execute(run_id)
