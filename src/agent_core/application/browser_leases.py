"""What a run needs from the hosted browser lease it holds (ADR-0127)."""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserRunState
from agent_core.domain.errors import NotFoundError
from agent_core.domain.runs import RunStatus
from agent_core.ports.browser import BrowserProvider, release_browser_run
from agent_core.ports.persistence import UnitOfWorkFactory

# Re-reads allowed when another process moves the run while it is classified.
_CONSISTENT_READ_ATTEMPTS = 3


async def release_browser_lease_if_done(
    provider: BrowserProvider,
    uow_factory: UnitOfWorkFactory,
    principal: Principal,
    run_id: UUID,
) -> None:
    """Seal and release a run's lease once an execution of it ends, if it is done.

    A run the owner approved before this read is queued or already running,
    possibly in another worker that reattached to the lease; it keeps it.
    """

    state = await browser_run_state(uow_factory, principal, run_id)
    if state is BrowserRunState.ENDED:
        await release_browser_run(provider, run_id)


async def browser_run_state(
    uow_factory: UnitOfWorkFactory,
    principal: Principal,
    run_id: UUID,
) -> BrowserRunState:
    """Classify a run for its browser lease.

    A running run, one queued to resume, or one parked on its own approval
    keeps the page. A parent waiting on a delegated child is also parked as
    waiting for approval but has no approval of its own: its child needs the
    profile, so it counts as ended, as does a run waiting on the user.

    Parking commits the approval with the run's status, and resolving an
    approval requeues the run in the same commit. The run is therefore read
    again after its approvals: an unchanged status means both reads agree.
    """

    async with uow_factory() as uow:
        for _attempt in range(_CONSISTENT_READ_ATTEMPTS):
            try:
                status = (await uow.runs.get(run_id, principal)).status
            except NotFoundError:
                return BrowserRunState.ENDED
            if status is not RunStatus.WAITING_FOR_APPROVAL:
                return _state_of(status)
            pending = await uow.approvals.list_pending(principal, run_id=run_id, limit=1)
            try:
                again = (await uow.runs.get(run_id, principal)).status
            except NotFoundError:
                return BrowserRunState.ENDED
            if again is RunStatus.WAITING_FOR_APPROVAL:
                return BrowserRunState.AWAITING_APPROVAL if pending else BrowserRunState.ENDED
    # Still moving after every attempt: keep the page rather than lose it.
    return BrowserRunState.RESUMING


def _state_of(status: RunStatus) -> BrowserRunState:
    if status is RunStatus.RUNNING:
        return BrowserRunState.RUNNING
    if status is RunStatus.QUEUED:
        return BrowserRunState.RESUMING
    return BrowserRunState.ENDED
