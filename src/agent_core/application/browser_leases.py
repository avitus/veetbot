"""What a run needs from the hosted browser lease it holds (ADR-0127)."""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserRunState
from agent_core.domain.errors import NotFoundError
from agent_core.domain.runs import RunStatus
from agent_core.ports.persistence import UnitOfWorkFactory


async def browser_run_state(
    uow_factory: UnitOfWorkFactory,
    principal: Principal,
    run_id: UUID,
) -> BrowserRunState:
    """Classify a run for its browser lease.

    Only a running run, or one parked on its own approval, keeps the page. A
    parent waiting on a delegated child is also parked as waiting for approval
    but has no approval of its own: its child needs the profile, so it ends.
    """

    async with uow_factory() as uow:
        try:
            run = await uow.runs.get(run_id, principal)
        except NotFoundError:
            return BrowserRunState.ENDED
        if run.status is RunStatus.RUNNING:
            return BrowserRunState.RUNNING
        if run.status is RunStatus.QUEUED:
            return BrowserRunState.RESUMING
        if run.status is RunStatus.WAITING_FOR_APPROVAL and await uow.approvals.list_pending(
            principal, run_id=run_id, limit=1
        ):
            return BrowserRunState.AWAITING_APPROVAL
    return BrowserRunState.ENDED
