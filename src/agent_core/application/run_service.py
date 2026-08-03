"""Run submission and reads shared by the CLI and future API."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from agent_core.application.session_service import SessionService
from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunStatus
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import RunDispatcher
from agent_core.ports.events import EventRepository
from agent_core.ports.repositories import AgentRepository, RunRepository


class RunService:
    def __init__(
        self,
        *,
        runs: RunRepository,
        agents: AgentRepository,
        events: EventRepository,
        dispatcher: RunDispatcher,
        sessions: SessionService,
        principal: Principal,
        clock: Clock,
        ids: IdFactory,
        cancel_active: Callable[[], None],
    ) -> None:
        self._runs = runs
        self._agents = agents
        self._events = events
        self._dispatcher = dispatcher
        self._sessions = sessions
        self._principal = principal
        self._clock = clock
        self._ids = ids
        self._cancel_active = cancel_active

    async def submit(self, prompt: str, session_id: UUID | None = None) -> UUID:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if session_id is None:
            session_id = await self._sessions.create()
        session = await self._sessions.get(session_id)
        agent = await self._agents.get_version(session.agent_id, session.agent_version)
        now = self._clock.now()
        run = Run(
            id=self._ids.new_id(),
            session_id=session.id,
            tenant_id=session.tenant_id,
            agent_id=session.agent_id,
            agent_version=session.agent_version,
            status=RunStatus.QUEUED,
            limits=agent.limits.model_copy(deep=True),
            scheduled_for=now,
            deadline_at=agent.limits.deadline_at,
            created_at=now,
            updated_at=now,
        )
        await self._runs.create(run)
        await self._events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=self._principal.principal_id,
                payload={"content": prompt},
            )
        )
        await self._events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="run.queued",
                actor_type="application",
                payload={"run_id": str(run.id)},
            )
        )
        await self._dispatcher.dispatch(run.id)
        return run.id

    async def get(self, run_id: UUID) -> Run:
        return await self._runs.get(run_id, self._principal)

    async def wait_terminal(self, run_id: UUID) -> Run:
        while True:
            run = await self.get(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            await self._clock.sleep(0.05)

    async def events(self, run_id: UUID) -> list[EventEnvelope]:
        run = await self.get(run_id)
        events = await self._events.list_after(run.session_id, 0, self._principal)
        return [event for event in events if event.run_id == run.id]

    def interrupt(self) -> None:
        self._cancel_active()
