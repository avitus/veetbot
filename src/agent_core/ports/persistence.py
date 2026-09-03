"""Unit-of-work boundary that groups repositories into one short transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Protocol, Self

from agent_core.domain.agents import Principal
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.ports.browser_authentications import BrowserAuthenticationRepository
from agent_core.ports.browser_grants import BrowserGrantRepository
from agent_core.ports.browser_profiles import BrowserProfileRepository
from agent_core.ports.delegations import DelegationRepository
from agent_core.ports.device_channel import DeviceIngestStore, DeviceInvocationStore
from agent_core.ports.devices import (
    DeviceRegistrationIdempotencyRepository,
    DeviceRegistry,
)
from agent_core.ports.dispatch import RunQueue
from agent_core.ports.events import EventRepository, ProcessEventRepository
from agent_core.ports.knowledge import KnowledgeStore
from agent_core.ports.mcp import MCPServerRepository
from agent_core.ports.memory import IntegratedEpisodeStore, MemoryStore, TraceStore
from agent_core.ports.notifications import NotificationOutbox
from agent_core.ports.personas import PersonaStore
from agent_core.ports.repositories import (
    AgentRepository,
    ApprovalRepository,
    ArtifactRepository,
    CapabilityEvaluationRepository,
    CheckpointRepository,
    ExportConsentRepository,
    IdempotencyRepository,
    MaintenanceRepository,
    PolicyProfileRepository,
    RunRepository,
    SessionDeletionRepository,
    SessionHistoryRepository,
    SessionRepository,
    ToolInvocationRepository,
    TrajectoryExportRepository,
    TrajectoryProjectionRepository,
    UsageRepository,
)
from agent_core.ports.schedules import (
    ScheduleAdmissionController,
    ScheduleIdempotencyRepository,
    ScheduleOccurrenceRepository,
    ScheduleRepository,
)
from agent_core.ports.skills import SkillRepository


class RepositoryUnitOfWork(Protocol):
    agents: AgentRepository
    approvals: ApprovalRepository
    policy_profiles: PolicyProfileRepository
    browser_profiles: BrowserProfileRepository
    browser_grants: BrowserGrantRepository
    browser_authentications: BrowserAuthenticationRepository
    process_events: ProcessEventRepository
    sessions: SessionRepository
    session_deletions: SessionDeletionRepository
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
    artifacts: ArtifactRepository
    maintenance: MaintenanceRepository
    skills: SkillRepository
    mcp_servers: MCPServerRepository
    memories: MemoryStore
    episodes: IntegratedEpisodeStore
    traces: TraceStore
    personas: PersonaStore
    knowledge: KnowledgeStore
    evaluations: CapabilityEvaluationRepository
    schedules: ScheduleRepository
    schedule_occurrences: ScheduleOccurrenceRepository
    schedule_idempotency: ScheduleIdempotencyRepository
    schedule_admission: ScheduleAdmissionController
    devices: DeviceRegistry
    device_registration_idempotency: DeviceRegistrationIdempotencyRepository
    device_invocations: DeviceInvocationStore
    device_ingest: DeviceIngestStore
    notification_outbox: NotificationOutbox
    delegations: DelegationRepository
    queue: RunQueue | None

    def on_rollback(self, callback: TransactionCallback) -> None:
        """Register best-effort cleanup when the surrounding transaction rolls back."""
        ...

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


class ScheduleUnitOfWork(Protocol):
    """Least-privilege repository surface used by scheduler components."""

    agents: AgentRepository
    process_events: ProcessEventRepository
    sessions: SessionRepository
    runs: RunRepository
    events: EventRepository
    history: SessionHistoryRepository
    checkpoints: CheckpointRepository
    schedules: ScheduleRepository
    schedule_occurrences: ScheduleOccurrenceRepository
    schedule_admission: ScheduleAdmissionController
    notification_outbox: NotificationOutbox
    queue: RunQueue | None

    def on_rollback(self, callback: TransactionCallback) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class ScheduleUnitOfWorkFactory(Protocol):
    def __call__(self) -> ScheduleUnitOfWork: ...

    def is_open(self) -> bool: ...


type CheckpointSeeder = Callable[
    [RepositoryUnitOfWork, Run, int | None, WorkerLease | None, Principal],
    Awaitable[RunCheckpoint],
]
type ScheduleCheckpointSeeder = Callable[
    [ScheduleUnitOfWork, Run, int | None, WorkerLease | None, Principal],
    Awaitable[RunCheckpoint],
]

type TransactionCallback = Callable[[], Awaitable[None]]
type TransactionCallbackRegistrar = Callable[[TransactionCallback], None]
