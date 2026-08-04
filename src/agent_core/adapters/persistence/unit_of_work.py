"""Short-lived repository units of work for memory and PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_core.ports.dispatch import RunQueue
from agent_core.ports.events import EventRepository, ProcessEventRepository
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

_UNIT_OF_WORK_DEPTH: ContextVar[int] = ContextVar("unit_of_work_depth", default=0)


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
    process_events: ProcessEventRepository
    sessions: SessionRepository
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
    maintenance: MaintenanceRepository
    queue: RunQueue | None


type PostgresRepositoryFactory = Callable[[AsyncSession], UnitOfWorkRepositories]


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
        self.process_events = repositories.process_events
        self.sessions = repositories.sessions
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
        self.maintenance = repositories.maintenance
        self.queue = repositories.queue
        self._depth_token: Token[int] | None = None

    async def __aenter__(self) -> MemoryUnitOfWork:
        self._depth_token = _enter_unit_of_work()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._depth_token is not None:
            _exit_unit_of_work(self._depth_token)
            self._depth_token = None


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
    ) -> None:
        self._maker = maker
        self._repository_factory = repository_factory
        self._session: AsyncSession | None = None
        self._depth_token: Token[int] | None = None

    async def __aenter__(self) -> PostgresUnitOfWork:
        session = self._maker()
        self._session = session
        repositories = self._repository_factory(session)
        self.agents = repositories.agents
        self.approvals = repositories.approvals
        self.policy_profiles = repositories.policy_profiles
        self.process_events = repositories.process_events
        self.sessions = repositories.sessions
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
        self.maintenance = repositories.maintenance
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
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            try:
                await self._session.close()
            finally:
                if self._depth_token is not None:
                    _exit_unit_of_work(self._depth_token)
                    self._depth_token = None


class PostgresUnitOfWorkFactory:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        repository_factory: PostgresRepositoryFactory,
    ) -> None:
        self._maker = maker
        self._repository_factory = repository_factory

    def __call__(self) -> PostgresUnitOfWork:
        return PostgresUnitOfWork(
            self._maker,
            self._repository_factory,
        )

    def is_open(self) -> bool:
        return _unit_of_work_is_open()
