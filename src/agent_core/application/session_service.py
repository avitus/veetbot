"""Session creation over repository ports."""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.events import EventRepository
from agent_core.ports.repositories import SessionRepository


class SessionService:
    def __init__(
        self,
        sessions: SessionRepository,
        events: EventRepository,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        default_agent: AgentSpec,
    ) -> None:
        self._sessions = sessions
        self._events = events
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._default_agent = default_agent

    async def create(self) -> UUID:
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
        await self._sessions.create(session)
        await self._events.append(
            NewEvent(
                session_id=session.id,
                run_id=None,
                event_type="session.created",
                actor_type="principal",
                actor_id=self._principal.principal_id,
                payload={"agent_id": str(session.agent_id)},
            )
        )
        return session.id

    async def get(self, session_id: UUID) -> Session:
        return await self._sessions.get(session_id, self._principal)
