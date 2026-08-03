"""Session creation over repository ports."""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class SessionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        default_agent: AgentSpec,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._default_agent = default_agent

    async def create(self) -> UUID:
        async with self._uow_factory() as uow:
            return await self.create_in(uow)

    async def create_in(self, uow: RepositoryUnitOfWork) -> UUID:
        """Create a session inside a caller-owned submission transaction."""

        now = self._clock.now()
        session = Session(
            id=self._ids.new_id(),
            tenant_id=self._principal.tenant_id,
            principal_id=self._principal.principal_id,
            agent_id=self._default_agent.id,
            agent_version=self._default_agent.version,
            status=SessionStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        await uow.sessions.create(session)
        await uow.events.append(
            NewEvent(
                session_id=session.id,
                run_id=None,
                event_type="session.created",
                payload_schema_version=2,
                actor_type="principal",
                actor_id=self._principal.principal_id,
                payload={"agent_id": str(session.agent_id), "title": session.title},
            )
        )
        return session.id

    async def get(self, session_id: UUID) -> Session:
        async with self._uow_factory() as uow:
            return await uow.sessions.get(session_id, self._principal)
