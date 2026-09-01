"""Short-lived repository units of work for memory and PostgreSQL."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import TracebackType

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.functions import func

from agent_core.ports.browser_authentications import BrowserAuthenticationRepository
from agent_core.ports.browser_grants import BrowserGrantRepository
from agent_core.ports.browser_profiles import BrowserProfileRepository
from agent_core.ports.delegations import DelegationRepository
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
from agent_core.ports.persistence import TransactionCallback, TransactionCallbackRegistrar
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

_UNIT_OF_WORK_DEPTH: ContextVar[int] = ContextVar("unit_of_work_depth", default=0)
logger = logging.getLogger(__name__)


def _enter_unit_of_work() -> Token[int]:
    return _UNIT_OF_WORK_DEPTH.set(_UNIT_OF_WORK_DEPTH.get() + 1)


def _exit_unit_of_work(token: Token[int]) -> None:
    _UNIT_OF_WORK_DEPTH.reset(token)


def _unit_of_work_is_open() -> bool:
    return _UNIT_OF_WORK_DEPTH.get() > 0


@dataclass(frozen=True, slots=True)
class UnitOfWorkRepositories:
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
    invocations: ToolInvocationRepository
    checkpoints: CheckpointRepository
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
    knowledge: KnowledgeStore
    evaluations: CapabilityEvaluationRepository
    schedules: ScheduleRepository
    schedule_occurrences: ScheduleOccurrenceRepository
    schedule_idempotency: ScheduleIdempotencyRepository
    schedule_admission: ScheduleAdmissionController
    devices: DeviceRegistry
    device_registration_idempotency: DeviceRegistrationIdempotencyRepository
    notification_outbox: NotificationOutbox
    delegations: DelegationRepository
    queue: RunQueue | None


type PostgresRepositoryFactory = Callable[
    [AsyncSession, TransactionCallbackRegistrar], UnitOfWorkRepositories
]


class MemoryUnitOfWork:
    """Group memory repositories without claiming transactional rollback.

    Mutations are retained even when the context exits with an exception. The
    adapter exists for deterministic evaluation; PostgreSQL is the tier that
    supplies atomic commit and rollback.
    """

    def __init__(
        self,
        repositories: UnitOfWorkRepositories,
    ) -> None:
        self.agents = repositories.agents
        self.approvals = repositories.approvals
        self.policy_profiles = repositories.policy_profiles
        self.browser_profiles = repositories.browser_profiles
        self.browser_grants = repositories.browser_grants
        self.browser_authentications = repositories.browser_authentications
        self.process_events = repositories.process_events
        self.sessions = repositories.sessions
        self.session_deletions = repositories.session_deletions
        self.runs = repositories.runs
        self.events = repositories.events
        self.invocations = repositories.invocations
        self.checkpoints = repositories.checkpoints
        self.idempotency = repositories.idempotency
        self.usage = repositories.usage
        self.history = repositories.history
        self.trajectory = repositories.trajectory
        self.export_consent = repositories.export_consent
        self.trajectory_exports = repositories.trajectory_exports
        self.artifacts = repositories.artifacts
        self.maintenance = repositories.maintenance
        self.skills = repositories.skills
        self.mcp_servers = repositories.mcp_servers
        self.memories = repositories.memories
        self.episodes = repositories.episodes
        self.traces = repositories.traces
        self.knowledge = repositories.knowledge
        self.evaluations = repositories.evaluations
        self.schedules = repositories.schedules
        self.schedule_occurrences = repositories.schedule_occurrences
        self.schedule_idempotency = repositories.schedule_idempotency
        self.schedule_admission = repositories.schedule_admission
        self.devices = repositories.devices
        self.device_registration_idempotency = repositories.device_registration_idempotency
        self.notification_outbox = repositories.notification_outbox
        self.delegations = repositories.delegations
        self.queue = repositories.queue
        self._depth_token: Token[int] | None = None
        self._rollback_callbacks: list[TransactionCallback] = []

    def on_rollback(self, callback: TransactionCallback) -> None:
        self._rollback_callbacks.append(callback)

    async def __aenter__(self) -> MemoryUnitOfWork:
        self._depth_token = _enter_unit_of_work()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self._run_rollback_callbacks()
        else:
            self._rollback_callbacks.clear()
        if self._depth_token is not None:
            _exit_unit_of_work(self._depth_token)
            self._depth_token = None

    async def _run_rollback_callbacks(self) -> None:
        callbacks = list(self._rollback_callbacks)
        self._rollback_callbacks.clear()
        for callback in reversed(callbacks):
            try:
                await callback()
            except BaseException:
                logger.exception("memory_transaction_rollback_callback_failed")


class MemoryUnitOfWorkFactory:
    def __init__(self, repositories: UnitOfWorkRepositories) -> None:
        self._repositories = repositories

    def __call__(self) -> MemoryUnitOfWork:
        return MemoryUnitOfWork(self._repositories)

    def is_open(self) -> bool:
        return _unit_of_work_is_open()


class PostgresUnitOfWork:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        repository_factory: PostgresRepositoryFactory,
        tenant_id: str,
    ) -> None:
        self._maker = maker
        self._repository_factory = repository_factory
        self._tenant_id = tenant_id
        self._session: AsyncSession | None = None
        self._depth_token: Token[int] | None = None
        self._rollback_callbacks: list[TransactionCallback] = []

    def on_rollback(self, callback: TransactionCallback) -> None:
        self._rollback_callbacks.append(callback)

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("PostgreSQL unit of work is not active")
        return self._session

    async def __aenter__(self) -> PostgresUnitOfWork:
        session = self._maker()
        self._session = session
        await session.execute(
            select(func.set_config("agent_core.tenant_id", self._tenant_id, True))
        )
        repositories = self._repository_factory(session, self._rollback_callbacks.append)
        self.agents = repositories.agents
        self.approvals = repositories.approvals
        self.policy_profiles = repositories.policy_profiles
        self.browser_profiles = repositories.browser_profiles
        self.browser_grants = repositories.browser_grants
        self.browser_authentications = repositories.browser_authentications
        self.process_events = repositories.process_events
        self.sessions = repositories.sessions
        self.session_deletions = repositories.session_deletions
        self.runs = repositories.runs
        self.events = repositories.events
        self.history = repositories.history
        self.trajectory = repositories.trajectory
        self.checkpoints = repositories.checkpoints
        self.invocations = repositories.invocations
        self.idempotency = repositories.idempotency
        self.usage = repositories.usage
        self.export_consent = repositories.export_consent
        self.trajectory_exports = repositories.trajectory_exports
        self.artifacts = repositories.artifacts
        self.maintenance = repositories.maintenance
        self.skills = repositories.skills
        self.mcp_servers = repositories.mcp_servers
        self.memories = repositories.memories
        self.episodes = repositories.episodes
        self.traces = repositories.traces
        self.knowledge = repositories.knowledge
        self.evaluations = repositories.evaluations
        self.schedules = repositories.schedules
        self.schedule_occurrences = repositories.schedule_occurrences
        self.schedule_idempotency = repositories.schedule_idempotency
        self.schedule_admission = repositories.schedule_admission
        self.devices = repositories.devices
        self.device_registration_idempotency = repositories.device_registration_idempotency
        self.notification_outbox = repositories.notification_outbox
        self.delegations = repositories.delegations
        self.queue = repositories.queue
        self._depth_token = _enter_unit_of_work()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        try:
            if exc_type is None:
                try:
                    await self._session.commit()
                except BaseException:
                    await self._run_rollback_callbacks()
                    await self._session.rollback()
                    raise
                else:
                    self._rollback_callbacks.clear()
            else:
                await self._run_rollback_callbacks()
                await self._session.rollback()
        finally:
            try:
                await self._session.close()
            finally:
                self._session = None
                if self._depth_token is not None:
                    _exit_unit_of_work(self._depth_token)
                    self._depth_token = None

    async def _run_rollback_callbacks(self) -> None:
        callbacks = list(self._rollback_callbacks)
        self._rollback_callbacks.clear()
        for callback in reversed(callbacks):
            try:
                await callback()
            except BaseException:
                logger.exception(
                    "transaction_rollback_callback_failed",
                    extra={"tenant_id": self._tenant_id},
                )


class PostgresUnitOfWorkFactory:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        repository_factory: PostgresRepositoryFactory,
        tenant_id: str,
    ) -> None:
        self._maker = maker
        self._repository_factory = repository_factory
        self._tenant_id = tenant_id

    def __call__(self) -> PostgresUnitOfWork:
        return PostgresUnitOfWork(
            self._maker,
            self._repository_factory,
            self._tenant_id,
        )

    def is_open(self) -> bool:
        return _unit_of_work_is_open()
