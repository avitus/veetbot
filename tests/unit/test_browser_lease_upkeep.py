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
from agent_core.application.browser_leases import (
    browser_run_state,
    release_browser_lease_if_done,
)
from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserRunState
from agent_core.domain.errors import NotFoundError
from agent_core.domain.runs import Run, RunStatus
from agent_core.ports.browser import BrowserProvider
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.executor import RunExecutor
from agent_core.runtime.worker import DurableWorker
from tests.contract.support import NOW, principal, run


class _Runs:
    """A run whose status may change between reads, as another process commits."""

    def __init__(self, found: Run | None, later: tuple[RunStatus, ...] = ()) -> None:
        self._found = found
        self._later = list(later)

    async def get(self, run_id: UUID, owner: Principal) -> Run:
        del owner
        if self._found is None or self._found.id != run_id:
            raise NotFoundError("run not found")
        found = self._found
        if self._later:
            self._found = found.model_copy(update={"status": self._later.pop(0)})
        return found


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
    def __init__(
        self,
        found: Run | None = None,
        pending: int = 0,
        later: tuple[RunStatus, ...] = (),
    ) -> None:
        self.runs = _Runs(found, later)
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


def unit_of_work(
    found: Run | None = None,
    pending: int = 0,
    later: tuple[RunStatus, ...] = (),
) -> UnitOfWorkFactory:
    shared = _UnitOfWork(found, pending, later)
    return cast(UnitOfWorkFactory, lambda: shared)


class _Releases:
    def __init__(self) -> None:
        self.released: list[UUID] = []

    async def release_run(self, run_id: UUID) -> None:
        self.released.append(run_id)


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


async def test_an_approval_resolved_between_reads_is_not_read_as_ended() -> None:
    # The run parked on its approval; the owner approves while the reader looks,
    # so the approval is gone by the time it is read and the run is queued.
    found = run(status=RunStatus.WAITING_FOR_APPROVAL)
    racing = unit_of_work(found, pending=0, later=(RunStatus.QUEUED,))

    assert await browser_run_state(racing, principal(), found.id) is BrowserRunState.RESUMING


async def test_a_run_parking_while_read_is_not_read_as_ended() -> None:
    found = run(status=RunStatus.RUNNING)
    racing = unit_of_work(found, pending=1, later=(RunStatus.WAITING_FOR_APPROVAL,))

    assert await browser_run_state(racing, principal(), found.id) is BrowserRunState.RUNNING


@pytest.mark.parametrize(
    ("status", "pending_approvals", "released"),
    [
        # Still using the page: here or in another worker, approved and queued
        # to resume, or parked on its own approval.
        (RunStatus.RUNNING, 0, False),
        (RunStatus.QUEUED, 0, False),
        (RunStatus.WAITING_FOR_APPROVAL, 1, False),
        # Done with it: parked on a child or the user, or finished.
        (RunStatus.WAITING_FOR_APPROVAL, 0, True),
        (RunStatus.WAITING_FOR_USER, 0, True),
        (RunStatus.COMPLETED, 0, True),
        (RunStatus.CANCELLED, 0, True),
    ],
)
async def test_an_ended_execution_releases_the_lease_only_when_the_run_is_done_with_it(
    status: RunStatus,
    pending_approvals: int,
    released: bool,
) -> None:
    found = run(status=status)
    provider = _Releases()

    await release_browser_lease_if_done(
        cast(BrowserProvider, provider),
        unit_of_work(found, pending_approvals),
        principal(),
        found.id,
    )

    assert provider.released == ([found.id] if released else [])


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
