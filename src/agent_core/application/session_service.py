"""Session creation over repository ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.skills import SessionSkillCatalog
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.skills import SkillCatalog

type CloseSession = Callable[[UUID], Awaitable[None]]


async def bootstrap_session(
    uow: RepositoryUnitOfWork,
    ids: IdFactory,
    catalogs: SkillCatalog | None,
    close_session: CloseSession | None,
    agent: AgentSpec,
    principal: Principal,
) -> tuple[UUID, SessionSkillCatalog | None]:
    """Open rollback-safe ephemeral state before persisting a new session."""

    session_id = ids.new_id()
    if catalogs is not None:
        uow.on_rollback(lambda: catalogs.discard(session_id))
    if close_session is not None:
        uow.on_rollback(lambda: close_session(session_id))
    catalog = (
        None
        if catalogs is None
        else await catalogs.open(
            session_id,
            agent,
            principal,
        )
    )
    return session_id, catalog


class SessionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        default_agent: AgentSpec,
        catalogs: SkillCatalog | None = None,
        activate_session: Callable[[UUID], Awaitable[None]] | None = None,
        close_session: CloseSession | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._default_agent = default_agent
        self._catalogs = catalogs
        self._activate_session = activate_session
        self._close_session = close_session

    async def create(self) -> UUID:
        async with self._uow_factory() as uow:
            session_id = await self.create_in(uow)
        await self.activate(session_id)
        return session_id

    async def activate(self, session_id: UUID) -> None:
        if self._activate_session is not None:
            await self._activate_session(session_id)

    async def create_in(self, uow: RepositoryUnitOfWork) -> UUID:
        """Create inside a caller-owned transaction; activate after it commits."""

        now = self._clock.now()
        session_id, catalog = await bootstrap_session(
            uow,
            self._ids,
            self._catalogs,
            self._close_session,
            self._default_agent,
            self._principal,
        )
        session = Session(
            id=session_id,
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
                payload={
                    "agent_id": str(session.agent_id),
                    "title": session.title,
                    "skill_pins": (
                        []
                        if catalog is None
                        else [pin.model_dump(mode="json") for pin in catalog.pins]
                    ),
                    "dropped_skills": [] if catalog is None else list(catalog.dropped_names),
                },
            )
        )
        return session.id

    async def get(self, session_id: UUID) -> Session:
        async with self._uow_factory() as uow:
            return await uow.sessions.get(session_id, self._principal)
