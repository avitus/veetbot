"""Which runs keep a hosted browser lease, and the worker tick that keeps it (ADR-0127)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from itertools import pairwise
from types import TracebackType
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.browser_leases import browser_run_state
from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserRunState
from agent_core.domain.errors import NotFoundError
from agent_core.domain.runs import Run, RunStatus
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.executor import RunExecutor
from agent_core.runtime.worker import DurableWorker
from tests.contract.support import NOW, principal, run


class _Runs:
    def __init__(self, found: Run | None) -> None:
        self._found = found

    async def get(self, run_id: UUID, owner: Principal) -> Run:
        del owner
        if self._found is None or self._found.id != run_id:
            raise NotFoundError("run not found")
        return self._found


class _Approvals:
    def __init__(self, pending: int) -> None:
        self._pending = pending

    async def list_pending(
        self,
        owner: Principal,
        run_id: UUID | None = None,
        session_id: UUID | None = None,
        limit: int = 50,
        cursor: object | None = None,
    ) -> list[object]:
        del owner, run_id, session_id, cursor
        return [object()] * min(self._pending, limit)


class _Queue:
    async def claim(self, worker_id: str, classes: tuple[int, ...]) -> None:
        del worker_id, classes


class _UnitOfWork:
    def __init__(self, found: Run | None = None, pending: int = 0) -> None:
        self.runs = _Runs(found)
        self.approvals = _Approvals(pending)
        self.queue = _Queue()

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def unit_of_work(found: Run | None = None, pending: int = 0) -> UnitOfWorkFactory:
    return cast(UnitOfWorkFactory, lambda: _UnitOfWork(found, pending))


@pytest.mark.parametrize(
    ("status", "pending_approvals", "expected"),
    [
        (RunStatus.RUNNING, 0, BrowserRunState.RUNNING),
        (RunStatus.WAITING_FOR_APPROVAL, 1, BrowserRunState.AWAITING_APPROVAL),
        # A parent parked on a delegated child also waits "for approval", with
        # no approval of its own; the child needs the profile.
        (RunStatus.WAITING_FOR_APPROVAL, 0, BrowserRunState.ENDED),
        (RunStatus.QUEUED, 0, BrowserRunState.RESUMING),
        (RunStatus.WAITING_FOR_USER, 0, BrowserRunState.ENDED),
        (RunStatus.COMPLETED, 0, BrowserRunState.ENDED),
        (RunStatus.FAILED, 0, BrowserRunState.ENDED),
        (RunStatus.CANCELLED, 0, BrowserRunState.ENDED),
    ],
)
async def test_only_a_running_run_or_its_own_approval_keeps_the_browser(
    status: RunStatus,
    pending_approvals: int,
    expected: BrowserRunState,
) -> None:
    found = run(status=status)

    state = await browser_run_state(unit_of_work(found, pending_approvals), principal(), found.id)

    assert state is expected


async def test_an_unknown_run_needs_no_browser() -> None:
    state = await browser_run_state(unit_of_work(), principal(), run().id)

    assert state is BrowserRunState.ENDED


async def test_idle_worker_keeps_its_lease_upkeep_running_through_failures() -> None:
    clock = FixedClock(NOW)
    ticks: list[datetime] = []
    worker: DurableWorker | None = None

    async def upkeep() -> None:
        ticks.append(clock.now())
        if len(ticks) == 1:
            raise RuntimeError("isolated browser service unreachable")
        if len(ticks) == 3:
            assert worker is not None
            worker.stop()

    worker = DurableWorker(
        uow_factory=unit_of_work(),
        executor=cast(RunExecutor, object()),
        clock=clock,
        worker_id="worker-under-test",
        maintain_browser_leases=upkeep,
        browser_lease_upkeep_seconds=60,
    )

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert len(ticks) == 3
    assert all(later - earlier >= timedelta(seconds=60) for earlier, later in pairwise(ticks))
