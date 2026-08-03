"""Contract-backed single-process repositories for the Milestone 1 slice."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.runs import Run, RunFailure, RunStatus
from agent_core.domain.sessions import Session
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from agent_core.ports.determinism import Clock
from agent_core.ports.repositories import RunRepository, SessionRepository
from agent_core.runtime.state_machine import require_transition

MEMORY_ADAPTER_CAPABILITIES = frozenset(
    {
        "single_process_serialization",
        "tenant_scope",
        "monotonic_event_sequence",
        "append_only_events",
        "guarded_transitions",
        "idempotency_lookup",
    }
)
MEMORY_ADAPTER_GAPS = frozenset(
    {
        "cross_process_durability",
        "cross_repository_transactions",
        "crash_recovery",
        "concurrent_idempotency_deduplication",
    }
)


class InMemoryAgentRepository:
    def __init__(self) -> None:
        self._agents: dict[tuple[UUID, str], AgentSpec] = {}
        self._latest: dict[UUID, str] = {}
        self._lock = asyncio.Lock()

    async def put(self, agent: AgentSpec) -> None:
        async with self._lock:
            key = (agent.id, agent.version)
            existing = self._agents.get(key)
            if existing is not None and existing != agent:
                raise ConflictError("agent version already exists with different content")
            self._agents[key] = agent.model_copy(deep=True)
            self._latest[agent.id] = agent.version

    async def get_version(self, agent_id: UUID, agent_version: str) -> AgentSpec:
        async with self._lock:
            try:
                return self._agents[(agent_id, agent_version)].model_copy(deep=True)
            except KeyError as exc:
                raise NotFoundError("agent version not found") from exc

    async def latest_version(self, agent_id: UUID) -> AgentSpec:
        async with self._lock:
            try:
                version = self._latest[agent_id]
                return self._agents[(agent_id, version)].model_copy(deep=True)
            except KeyError as exc:
                raise NotFoundError("agent not found") from exc


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._sessions: dict[UUID, Session] = {}
        self._lock = asyncio.Lock()

    async def create(self, session: Session) -> None:
        async with self._lock:
            if session.id in self._sessions:
                raise ConflictError("session already exists")
            self._sessions[session.id] = session.model_copy(deep=True)

    async def get(self, session_id: UUID, principal: Principal) -> Session:
        async with self._lock:
            try:
                session = self._sessions[session_id]
            except KeyError as exc:
                raise NotFoundError("session not found") from exc
            if (
                session.tenant_id != principal.tenant_id
                or session.principal_id != principal.principal_id
            ):
                raise NotFoundError("session not found")
            return session.model_copy(deep=True)


class InMemoryRunRepository:
    def __init__(self, sessions: SessionRepository, clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock
        self._runs: dict[UUID, Run] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: Run) -> None:
        async with self._lock:
            if run.id in self._runs:
                raise ConflictError("run already exists")
            self._runs[run.id] = run.model_copy(deep=True)

    async def get(self, run_id: UUID, principal: Principal) -> Run:
        async with self._lock:
            try:
                run = self._runs[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise NotFoundError("run not found") from exc
        await self._sessions.get(run.session_id, principal)
        return run

    async def transition(
        self,
        run_id: UUID,
        expected_status: RunStatus,
        new_status: RunStatus,
        *,
        failure: object | None = None,
        final_message: str | None = None,
    ) -> Run:
        async with self._lock:
            try:
                current = self._runs[run_id]
            except KeyError as exc:
                raise NotFoundError("run not found") from exc
            if current.status is not expected_status:
                raise ConflictError(
                    f"expected {expected_status.value}, found {current.status.value}"
                )
            require_transition(current.status, new_status)
            typed_failure = None if failure is None else RunFailure.model_validate(failure)
            updated = current.model_copy(
                update={
                    "status": new_status,
                    "failure": typed_failure,
                    "final_message": final_message,
                    "updated_at": self._clock.now(),
                },
                deep=True,
            )
            self._runs[run_id] = updated
            return updated.model_copy(deep=True)

    async def update_counters(self, run: Run) -> None:
        async with self._lock:
            try:
                current = self._runs[run.id]
            except KeyError as exc:
                raise NotFoundError("run not found") from exc
            if current.status is not run.status:
                raise ConflictError("counter update may not change run status")
            updated = current.model_copy(
                update={
                    "step_count": run.step_count,
                    "model_call_count": run.model_call_count,
                    "tool_call_count": run.tool_call_count,
                    "usage": run.usage.model_copy(deep=True),
                    "updated_at": run.updated_at,
                },
                deep=True,
            )
            if run != updated:
                raise ConflictError("counter update may change only counters and usage")
            self._runs[run.id] = updated


class InMemoryEventRepository:
    def __init__(self, sessions: SessionRepository, clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock
        self._events: dict[UUID, list[EventEnvelope]] = defaultdict(list)
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def append(self, event: NewEvent) -> EventEnvelope:
        async with self._lock:
            stream = self._events[event.session_id]
            envelope = EventEnvelope(
                id=self._next_id,
                sequence=len(stream) + 1,
                created_at=self._clock.now(),
                **event.model_dump(),
            )
            self._next_id += 1
            stream.append(envelope)
            return envelope.model_copy(deep=True)

    async def list_after(
        self, session_id: UUID, sequence: int, principal: Principal
    ) -> list[EventEnvelope]:
        await self._sessions.get(session_id, principal)
        async with self._lock:
            return [
                event.model_copy(deep=True)
                for event in self._events[session_id]
                if event.sequence > sequence
            ]


class InMemoryToolInvocationRepository:
    def __init__(self, runs: RunRepository) -> None:
        self._runs = runs
        self._invocations: dict[UUID, ToolInvocation] = {}
        self._idempotency: dict[tuple[UUID, str], UUID] = {}
        self._lock = asyncio.Lock()

    async def create(self, invocation: ToolInvocation) -> ToolInvocation:
        async with self._lock:
            if invocation.id in self._invocations:
                raise ConflictError("tool invocation already exists")
            key = (invocation.run_id, invocation.idempotency_key)
            existing_id = self._idempotency.get(key)
            if existing_id is not None:
                return self._invocations[existing_id].model_copy(deep=True)
            self._invocations[invocation.id] = invocation.model_copy(deep=True)
            self._idempotency[key] = invocation.id
            return invocation.model_copy(deep=True)

    async def find_by_idempotency_key(
        self, run_id: UUID, idempotency_key: str
    ) -> ToolInvocation | None:
        async with self._lock:
            invocation_id = self._idempotency.get((run_id, idempotency_key))
            if invocation_id is None:
                return None
            return self._invocations[invocation_id].model_copy(deep=True)

    async def transition(
        self,
        invocation_id: UUID,
        expected_status: ToolInvocationStatus,
        invocation: ToolInvocation,
    ) -> ToolInvocation:
        async with self._lock:
            try:
                current = self._invocations[invocation_id]
            except KeyError as exc:
                raise NotFoundError("tool invocation not found") from exc
            if current.status is not expected_status:
                raise ConflictError(
                    f"expected {expected_status.value}, found {current.status.value}"
                )
            if invocation.id != invocation_id or invocation.run_id != current.run_id:
                raise ConflictError("tool invocation identity cannot change")
            allowed: dict[ToolInvocationStatus, set[ToolInvocationStatus]] = {
                ToolInvocationStatus.PROPOSED: {
                    ToolInvocationStatus.AUTHORIZED,
                    ToolInvocationStatus.DENIED,
                },
                ToolInvocationStatus.AUTHORIZED: {ToolInvocationStatus.RUNNING},
                ToolInvocationStatus.WAITING_FOR_APPROVAL: {
                    ToolInvocationStatus.AUTHORIZED,
                    ToolInvocationStatus.DENIED,
                },
                ToolInvocationStatus.RUNNING: {
                    ToolInvocationStatus.RUNNING,
                    ToolInvocationStatus.SUCCEEDED,
                    ToolInvocationStatus.FAILED,
                    ToolInvocationStatus.UNCERTAIN,
                },
                ToolInvocationStatus.SUCCEEDED: set(),
                ToolInvocationStatus.FAILED: set(),
                ToolInvocationStatus.DENIED: set(),
                ToolInvocationStatus.UNCERTAIN: set(),
            }
            if invocation.status not in allowed[current.status]:
                raise ConflictError(
                    f"invalid tool transition {current.status.value}->{invocation.status.value}"
                )
            updated = current.model_copy(
                update={
                    "status": invocation.status,
                    "effect_sent_at": invocation.effect_sent_at,
                    "outcome": (
                        None
                        if invocation.outcome is None
                        else invocation.outcome.model_copy(deep=True)
                    ),
                    "updated_at": invocation.updated_at,
                },
                deep=True,
            )
            if invocation != updated:
                raise ConflictError("tool transition may not change immutable fields")
            self._invocations[invocation_id] = updated
            return updated.model_copy(deep=True)

    async def list_for_run(self, run_id: UUID, principal: Principal) -> list[ToolInvocation]:
        await self._runs.get(run_id, principal)
        async with self._lock:
            rows = [
                invocation.model_copy(deep=True)
                for invocation in self._invocations.values()
                if invocation.run_id == run_id
            ]
            return sorted(rows, key=lambda row: (row.step_number, row.created_at, row.id.int))
