"""Run submission and reads shared by the CLI and future API."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

from agent_core.application.session_service import SessionService
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.persistence import IdempotencyRecord
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunStatus
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import RunDispatcher
from agent_core.ports.persistence import (
    CheckpointSeeder,
    RepositoryUnitOfWork,
    UnitOfWorkFactory,
)

type CancelParkedRun = Callable[[RepositoryUnitOfWork, Run, str], Awaitable[Run]]


class _ExistingIdempotentRunError(Exception):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id


class RunService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        dispatcher: RunDispatcher,
        sessions: SessionService,
        principal: Principal,
        clock: Clock,
        ids: IdFactory,
        cancel_active: Callable[[UUID | None], None],
        seed_checkpoint: CheckpointSeeder,
        cancel_parked_run: CancelParkedRun,
        trajectory_export_enabled: bool = False,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher
        self._sessions = sessions
        self._principal = principal
        self._clock = clock
        self._ids = ids
        self._cancel_active = cancel_active
        self._seed_checkpoint = seed_checkpoint
        self._cancel_parked_run = cancel_parked_run
        self._trajectory_export_enabled = trajectory_export_enabled

    async def submit(
        self,
        prompt: str,
        session_id: UUID | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> UUID:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        requested_session_id = session_id
        now = self._clock.now()
        request_hash = hashlib.sha256(f"v1:{requested_session_id}:{prompt}".encode()).hexdigest()
        try:
            async with self._uow_factory() as uow:
                if idempotency_key is not None:
                    existing = await uow.idempotency.get(
                        idempotency_key,
                        self._principal.tenant_id,
                        self._principal.principal_id,
                    )
                    if existing is not None:
                        if existing.request_hash != request_hash:
                            raise ConflictError(
                                "idempotency key was reused for a different request"
                            )
                        return existing.run_id
                if session_id is None:
                    session_id = await self._sessions.create_in(uow)
                session = await uow.sessions.get(session_id, self._principal)
                agent = await uow.agents.get_version(session.agent_id, session.agent_version)
                consent = await uow.export_consent.get(
                    self._principal.tenant_id,
                    self._principal.principal_id,
                )
                run = Run(
                    id=self._ids.new_id(),
                    session_id=session.id,
                    tenant_id=session.tenant_id,
                    principal_scopes=set(self._principal.scopes),
                    agent_id=session.agent_id,
                    agent_version=session.agent_version,
                    status=RunStatus.QUEUED,
                    limits=agent.limits.model_copy(deep=True),
                    priority=0,
                    scheduled_for=now,
                    deadline_at=agent.limits.deadline_at,
                    export_consent=(
                        self._trajectory_export_enabled and consent is not None and consent.active
                    ),
                    created_at=now,
                    updated_at=now,
                )
                if uow.queue is None:
                    await uow.runs.create(run)
                else:
                    await uow.queue.enqueue(run, priority=run.priority, scheduled_for=now)
                user_event = await uow.events.append(
                    NewEvent(
                        session_id=run.session_id,
                        run_id=run.id,
                        event_type="user.message.created",
                        actor_type="principal",
                        actor_id=self._principal.principal_id,
                        payload={"content": prompt},
                    )
                )
                await uow.runs.set_seed_event_sequence(run.id, user_event.sequence)
                await uow.events.append(
                    NewEvent(
                        session_id=run.session_id,
                        run_id=run.id,
                        event_type="run.queued",
                        actor_type="application",
                        payload={"run_id": str(run.id), "priority": run.priority},
                    )
                )
                await self._seed_checkpoint(
                    uow,
                    run,
                    user_event.sequence,
                    None,
                    self._principal,
                )
                if idempotency_key is not None:
                    record = await uow.idempotency.create(
                        IdempotencyRecord(
                            key=idempotency_key,
                            tenant_id=self._principal.tenant_id,
                            principal_id=self._principal.principal_id,
                            request_hash=request_hash,
                            run_id=run.id,
                            created_at=now,
                            expires_at=now + timedelta(hours=24),
                        )
                    )
                    if record.run_id != run.id:
                        raise _ExistingIdempotentRunError(record.run_id)
        except _ExistingIdempotentRunError as duplicate:
            return duplicate.run_id
        await self._dispatcher.dispatch(run.id)
        return run.id

    async def get(self, run_id: UUID) -> Run:
        async with self._uow_factory() as uow:
            return await uow.runs.get(run_id, self._principal)

    async def wait_terminal(self, run_id: UUID) -> Run:
        while True:
            run = await self.get(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            await self._clock.sleep(0.05)

    async def events(self, run_id: UUID) -> list[EventEnvelope]:
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, self._principal)
            events = await uow.events.list_after(run.session_id, 0, self._principal)
            return [event for event in events if event.run_id == run.id]

    async def cancel(self, run_id: UUID) -> Run:
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, self._principal)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            if run.status in {RunStatus.QUEUED, RunStatus.WAITING_FOR_APPROVAL}:
                return await self._cancel_parked_run(uow, run, self._principal.principal_id)
            try:
                requested = await uow.runs.request_cancellation(run.id, run.status)
            except ConflictError:
                refreshed = await uow.runs.get(run_id, self._principal)
                if refreshed.status in TERMINAL_RUN_STATUSES:
                    return refreshed
                raise
        self._cancel_active(run_id)
        return requested

    def interrupt(self) -> None:
        self._cancel_active(None)
