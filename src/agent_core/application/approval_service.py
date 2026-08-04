"""Approval reads, guarded resolution, expiry, and run resumption."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import (
    ApprovalRequest,
    ApprovalResolutionState,
    ApprovalResolutionType,
)
from agent_core.domain.errors import AuthorizationError, ConflictError, NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.runs import Run, RunStatus
from agent_core.ports.determinism import Clock
from agent_core.ports.dispatch import RunDispatcher
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

type ResumeWaitingRun = Callable[[RepositoryUnitOfWork, Run], Awaitable[Run]]


class ApprovalService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        dispatcher: RunDispatcher,
        principal: Principal,
        clock: Clock,
        resume_waiting_run: ResumeWaitingRun,
        self_approval_enabled: bool = True,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher
        self._principal = principal
        self._clock = clock
        self._resume_waiting_run = resume_waiting_run
        self._self_approval_enabled = self_approval_enabled

    def _require(self, scope: str) -> None:
        if scope not in self._principal.scopes:
            raise AuthorizationError(f"missing required scope: {scope}")

    async def get(self, approval_id: UUID) -> ApprovalRequest:
        self._require("approval.read")
        async with self._uow_factory() as uow:
            return await uow.approvals.get(approval_id, self._principal)

    async def list_pending(
        self,
        *,
        run_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[ApprovalRequest]:
        self._require("approval.read")
        if not 1 <= limit <= 100:
            raise ValueError("approval list limit must be between 1 and 100")
        async with self._uow_factory() as uow:
            return await uow.approvals.list_pending(
                self._principal, run_id=run_id, limit=limit, cursor=cursor
            )

    async def resolve(
        self,
        approval_id: UUID,
        resolution: ApprovalResolutionType,
        *,
        reason: str | None = None,
    ) -> ApprovalRequest:
        self._require("approval.resolve")
        dispatch_run: UUID | None = None
        async with self._uow_factory() as uow:
            visible = await uow.approvals.get(approval_id, self._principal)
            if (
                not self._self_approval_enabled
                and visible.principal_id == self._principal.principal_id
            ):
                raise AuthorizationError("approval requires a distinct resolver")
            outcome = await uow.approvals.resolve(approval_id, self._principal, resolution, reason)
            if outcome.state is ApprovalResolutionState.ALREADY_RESOLVED_DIFFERENTLY:
                raise ConflictError("approval was already resolved differently")
            if outcome.state is ApprovalResolutionState.APPLIED:
                owner = Principal(
                    tenant_id=outcome.approval.tenant_id,
                    principal_id=outcome.approval.principal_id,
                )
                run = await uow.runs.get(outcome.approval.run_id, owner)
                await uow.events.append(
                    NewEvent(
                        session_id=run.session_id,
                        run_id=run.id,
                        event_type="approval.resolved",
                        actor_type="principal",
                        actor_id=self._principal.principal_id,
                        payload={
                            "approval_id": str(outcome.approval.id),
                            "resolution": resolution.value,
                        },
                    )
                )
                if run.status is RunStatus.WAITING_FOR_APPROVAL:
                    await self._resume_waiting_run(uow, run)
                    dispatch_run = run.id
        if dispatch_run is not None:
            await self._dispatcher.resume(dispatch_run)
        return outcome.approval

    async def expire_due(self, *, limit: int = 100) -> int:
        dispatch: list[UUID] = []
        async with self._uow_factory() as uow:
            expired = await uow.approvals.expire_due(self._clock.now(), limit)
            for approval in expired:
                try:
                    owner = Principal(
                        tenant_id=approval.tenant_id,
                        principal_id=approval.principal_id,
                    )
                    run = await uow.runs.get(approval.run_id, owner)
                except NotFoundError:
                    continue
                await uow.events.append(
                    NewEvent(
                        session_id=run.session_id,
                        run_id=run.id,
                        event_type="approval.resolved",
                        actor_type="application",
                        payload={"approval_id": str(approval.id), "resolution": "expired"},
                    )
                )
                if run.status is RunStatus.WAITING_FOR_APPROVAL:
                    await self._resume_waiting_run(uow, run)
                    dispatch.append(run.id)
        for run_id in dispatch:
            await self._dispatcher.resume(run_id)
        return len(expired)
