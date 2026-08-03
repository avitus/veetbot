"""Short-lived repository units of work for memory and PostgreSQL."""

from __future__ import annotations

from contextvars import ContextVar, Token
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryCheckpointRepository,
    InMemoryEventRepository,
    InMemoryIdempotencyRepository,
    InMemoryMaintenanceRepository,
    InMemoryRunRepository,
    InMemorySessionHistoryRepository,
    InMemorySessionRepository,
    InMemoryToolInvocationRepository,
    InMemoryTrajectoryProjectionRepository,
    InMemoryUsageRepository,
)
from agent_core.adapters.persistence.projections import (
    PostgresSessionHistoryRepository,
    PostgresTrajectoryProjectionRepository,
)
from agent_core.adapters.persistence.queue import PostgresRunQueue
from agent_core.adapters.persistence.repositories import (
    PostgresAgentRepository,
    PostgresCheckpointRepository,
    PostgresEventRepository,
    PostgresIdempotencyRepository,
    PostgresMaintenanceRepository,
    PostgresRunRepository,
    PostgresSessionRepository,
    PostgresToolInvocationRepository,
    PostgresUsageRepository,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
from agent_core.ports.determinism import Clock

_UNIT_OF_WORK_DEPTH: ContextVar[int] = ContextVar("unit_of_work_depth", default=0)


def _enter_unit_of_work() -> Token[int]:
    return _UNIT_OF_WORK_DEPTH.set(_UNIT_OF_WORK_DEPTH.get() + 1)


def _exit_unit_of_work(token: Token[int]) -> None:
    _UNIT_OF_WORK_DEPTH.reset(token)


def _unit_of_work_is_open() -> bool:
    return _UNIT_OF_WORK_DEPTH.get() > 0


class MemoryUnitOfWork:
    """Group memory repositories without claiming transactional rollback.

    Mutations are retained even when the context exits with an exception. The
    adapter exists for deterministic evaluation; PostgreSQL is the tier that
    supplies atomic commit and rollback.
    """

    def __init__(
        self,
        *,
        agents: InMemoryAgentRepository,
        sessions: InMemorySessionRepository,
        runs: InMemoryRunRepository,
        events: InMemoryEventRepository,
        invocations: InMemoryToolInvocationRepository,
        checkpoints: InMemoryCheckpointRepository,
        idempotency: InMemoryIdempotencyRepository,
        usage: InMemoryUsageRepository,
        history: InMemorySessionHistoryRepository,
        trajectory: InMemoryTrajectoryProjectionRepository,
        maintenance: InMemoryMaintenanceRepository,
    ) -> None:
        self.agents = agents
        self.sessions = sessions
        self.runs = runs
        self.events = events
        self.invocations = invocations
        self.checkpoints = checkpoints
        self.idempotency = idempotency
        self.usage = usage
        self.history = history
        self.trajectory = trajectory
        self.maintenance = maintenance
        self.queue = None
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
    def __init__(
        self,
        *,
        agents: InMemoryAgentRepository,
        sessions: InMemorySessionRepository,
        runs: InMemoryRunRepository,
        events: InMemoryEventRepository,
        invocations: InMemoryToolInvocationRepository,
        clock: Clock,
    ) -> None:
        self._agents = agents
        self._sessions = sessions
        self._runs = runs
        self._events = events
        self._invocations = invocations
        self._checkpoints = InMemoryCheckpointRepository()
        self._idempotency = InMemoryIdempotencyRepository(clock)
        self._usage = InMemoryUsageRepository(runs)
        self._history = InMemorySessionHistoryRepository(events)
        self._trajectory = InMemoryTrajectoryProjectionRepository(events)
        self._maintenance = InMemoryMaintenanceRepository()

    def __call__(self) -> MemoryUnitOfWork:
        return MemoryUnitOfWork(
            agents=self._agents,
            sessions=self._sessions,
            runs=self._runs,
            events=self._events,
            invocations=self._invocations,
            checkpoints=self._checkpoints,
            idempotency=self._idempotency,
            usage=self._usage,
            history=self._history,
            trajectory=self._trajectory,
            maintenance=self._maintenance,
        )

    def is_open(self) -> bool:
        return _unit_of_work_is_open()


class PostgresUnitOfWork:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        clock: Clock,
        upcasters: EventUpcasterRegistry,
        *,
        lease_seconds: float,
        max_attempts: int,
    ) -> None:
        self._maker = maker
        self._clock = clock
        self._upcasters = upcasters
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._session: AsyncSession | None = None
        self._depth_token: Token[int] | None = None

    async def __aenter__(self) -> PostgresUnitOfWork:
        session = self._maker()
        self._session = session
        self.agents = PostgresAgentRepository(session, self._clock)
        self.sessions = PostgresSessionRepository(session)
        self.runs = PostgresRunRepository(session, self._clock)
        self.events = PostgresEventRepository(session, self._clock, self._upcasters)
        self.history = PostgresSessionHistoryRepository(session, self._clock, self._upcasters)
        self.trajectory = PostgresTrajectoryProjectionRepository(
            session, self._clock, self._upcasters
        )
        self.checkpoints = PostgresCheckpointRepository(session, self._clock, self.history)
        self.invocations = PostgresToolInvocationRepository(session, self.runs)
        self.idempotency = PostgresIdempotencyRepository(session, self._clock)
        self.usage = PostgresUsageRepository(session)
        self.maintenance = PostgresMaintenanceRepository(session)
        self.queue = PostgresRunQueue(
            session,
            self._clock,
            self.events,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
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
        clock: Clock,
        upcasters: EventUpcasterRegistry,
        *,
        lease_seconds: float,
        max_attempts: int,
    ) -> None:
        self._maker = maker
        self._clock = clock
        self._upcasters = upcasters
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    def __call__(self) -> PostgresUnitOfWork:
        return PostgresUnitOfWork(
            self._maker,
            self._clock,
            self._upcasters,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )

    def is_open(self) -> bool:
        return _unit_of_work_is_open()
