"""Execution permits backed by the runtime's nonrefundable checkpoint reservation."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from agent_core.domain.memory import RecallQuery

EXHAUSTED = (
    "Memory recall reached this run's search allowance. This does not mean no memory exists."
)


class RecallBudgetExceededError(Exception):
    """No search may begin, or an admitted search reached its shared deadline."""


@dataclass
class RecallExecutionPermit:
    tenant_id: str
    principal_id: str
    session_id: UUID
    run_id: UUID
    remaining: int
    deadline_at: datetime
    now: Callable[[], datetime]
    elapsed_time: Callable[[], float]
    closed: bool = False
    _elapsed_deadline: float = field(init=False)

    def __post_init__(self) -> None:
        self._elapsed_deadline = self.elapsed_time() + max(
            0, (self.deadline_at - self.now()).total_seconds()
        )

    def seconds_left(self) -> float:
        return min(
            self._elapsed_deadline - self.elapsed_time(),
            (self.deadline_at - self.now()).total_seconds(),
        )


_permit: ContextVar[RecallExecutionPermit | None] = ContextVar("recall_execution", default=None)
# Nested People -> belief calls share an attempt, but an inherited child task
# must acquire its own slot. A task identity cannot transfer a search allowance.
_entered: ContextVar[object | None] = ContextVar("recall_execution_task", default=None)


@contextmanager
def recall_phase(permit: RecallExecutionPermit | None) -> Iterator[None]:
    token = _permit.set(permit)
    try:
        yield
    finally:
        if permit is not None:
            permit.closed = True
        _permit.reset(token)


@asynccontextmanager
async def recall_search(
    query: RecallQuery, *, session_id: UUID, run_id: UUID | None
) -> AsyncIterator[None]:
    permit = _permit.get()
    if permit is None:
        yield
        return
    if (
        permit.closed
        or (query.tenant_id, query.principal_id, session_id, run_id)
        != (permit.tenant_id, permit.principal_id, permit.session_id, permit.run_id)
        or permit.seconds_left() <= 0
    ):
        raise RecallBudgetExceededError(EXHAUSTED)
    task = asyncio.current_task()
    if _entered.get() is task:
        yield
        return
    if permit.remaining <= 0:
        raise RecallBudgetExceededError(EXHAUSTED)
    permit.remaining -= 1  # No await between admission and consumption.
    token = _entered.set(task)
    timeout = asyncio.timeout(max(0, permit.seconds_left()))
    try:
        try:
            async with timeout:
                yield
        except TimeoutError as exc:
            if timeout.expired():
                raise RecallBudgetExceededError(EXHAUSTED) from exc
            raise
    finally:
        _entered.reset(token)
