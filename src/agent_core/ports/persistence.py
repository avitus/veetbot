"""Unit-of-work boundary that groups repositories into one short transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Protocol, Self

from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.ports.dispatch import RunQueue
from agent_core.ports.events import EventRepository
from agent_core.ports.repositories import (
    AgentRepository,
    ApprovalRepository,
    CheckpointRepository,
    ExportConsentRepository,
    IdempotencyRepository,
    MaintenanceRepository,
    PolicyProfileRepository,
    RunRepository,
    SessionHistoryRepository,
    SessionRepository,
    ToolInvocationRepository,
    TrajectoryExportRepository,
    TrajectoryProjectionRepository,
    UsageRepository,
)


class RepositoryUnitOfWork(Protocol):
    agents: AgentRepository
    approvals: ApprovalRepository
    policy_profiles: PolicyProfileRepository
    sessions: SessionRepository
    runs: RunRepository
    events: EventRepository
    checkpoints: CheckpointRepository
    invocations: ToolInvocationRepository
    idempotency: IdempotencyRepository
    usage: UsageRepository
    history: SessionHistoryRepository
    trajectory: TrajectoryProjectionRepository
    export_consent: ExportConsentRepository
    trajectory_exports: TrajectoryExportRepository
    maintenance: MaintenanceRepository
    queue: RunQueue | None

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> RepositoryUnitOfWork: ...

    def is_open(self) -> bool: ...


type CheckpointSeeder = Callable[
    [RepositoryUnitOfWork, Run, int | None, WorkerLease | None],
    Awaitable[RunCheckpoint],
]
