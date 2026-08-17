"""Contract-backed single-process repositories for the Milestone 1 slice."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from agent_core.adapters.persistence.conversation import conversation_items
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import (
    ApprovalCursor,
    ApprovalRequest,
    ApprovalResolutionOutcome,
    ApprovalResolutionState,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import EventEnvelope, NewEvent, ProcessEvent
from agent_core.domain.messages import ProviderPin
from agent_core.domain.persistence import (
    IdempotencyRecord,
    ModelCallRecord,
    SessionHistory,
    TrajectoryProjection,
    UsageRollup,
    WorkerLease,
)
from agent_core.domain.policies import PolicyProfileRecord
from agent_core.domain.runs import (
    TERMINAL_RUN_STATUSES,
    Run,
    RunCheckpoint,
    RunFailure,
    RunKind,
    RunStatus,
    RunUsage,
)
from agent_core.domain.sessions import Session, SessionCursor, SessionStatus, conversation_title
from agent_core.domain.tools import (
    ALLOWED_TOOL_TRANSITIONS,
    ToolInvocation,
    ToolInvocationStatus,
)
from agent_core.domain.trajectory import ArtifactRef, ExportConsent, TrajectoryExport
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

    async def set_title_if_missing(
        self, session_id: UUID, principal: Principal, title: str
    ) -> Session:
        normalized = conversation_title(title)
        if normalized is None:
            raise ValueError("session title must contain text")
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
            if session.title is None:
                session = session.model_copy(update={"title": normalized}, deep=True)
                self._sessions[session_id] = session
            return session.model_copy(deep=True)

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: SessionCursor | None = None,
    ) -> list[Session]:
        async with self._lock:
            rows = [
                session.model_copy(deep=True)
                for session in self._sessions.values()
                if session.tenant_id == principal.tenant_id
                and session.principal_id == principal.principal_id
                and (
                    cursor is None
                    or session.updated_at < cursor.updated_at
                    or (session.updated_at == cursor.updated_at and session.id.int < cursor.id.int)
                )
            ]
        rows.sort(key=lambda session: (session.updated_at, session.id.int), reverse=True)
        return rows[:limit]

    async def touch(self, session_id: UUID, touched_at: datetime) -> None:
        async with self._lock:
            try:
                session = self._sessions[session_id]
            except KeyError as exc:
                raise NotFoundError("session not found") from exc
            self._sessions[session_id] = session.model_copy(
                update={"updated_at": max(session.updated_at, touched_at)}
            )

    async def close(
        self, session_id: UUID, principal: Principal, closed_at: datetime
    ) -> tuple[Session, bool]:
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
            if session.status is not SessionStatus.ACTIVE:
                return session.model_copy(deep=True), False
            updated = session.model_copy(
                update={"status": SessionStatus.CLOSED, "updated_at": closed_at}, deep=True
            )
            self._sessions[session_id] = updated
            return updated.model_copy(deep=True), True


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
            if (
                run.parent_run_id is not None
                and run.kind is RunKind.SKILL_REVIEW
                and any(
                    candidate.parent_run_id == run.parent_run_id and candidate.kind is run.kind
                    for candidate in self._runs.values()
                )
            ):
                raise ConflictError("parent already has a child run of this kind")
            self._runs[run.id] = run.model_copy(deep=True)

    async def get(self, run_id: UUID, principal: Principal) -> Run:
        async with self._lock:
            try:
                run = self._runs[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise NotFoundError("run not found") from exc
        await self._sessions.get(run.session_id, principal)
        return run

    async def active_for_session(self, session_id: UUID, principal: Principal) -> Run | None:
        await self._sessions.get(session_id, principal)
        async with self._lock:
            rows = [
                run.model_copy(deep=True)
                for run in self._runs.values()
                if run.session_id == session_id and run.status not in TERMINAL_RUN_STATUSES
            ]
        if len(rows) > 1:
            raise ConflictError("session has multiple active runs")
        return rows[0] if rows else None

    async def latest_for_session(self, session_id: UUID, principal: Principal) -> Run | None:
        await self._sessions.get(session_id, principal)
        async with self._lock:
            rows = [
                run.model_copy(deep=True)
                for run in self._runs.values()
                if run.session_id == session_id
            ]
        return max(rows, key=lambda run: (run.created_at, run.id.int), default=None)

    async def latest_for_sessions(
        self, session_ids: list[UUID], principal: Principal
    ) -> dict[UUID, Run]:
        authorized_ids: set[UUID] = set()
        for session_id in session_ids:
            try:
                await self._sessions.get(session_id, principal)
            except NotFoundError:
                continue
            authorized_ids.add(session_id)
        async with self._lock:
            latest: dict[UUID, Run] = {}
            for run in self._runs.values():
                if run.session_id not in authorized_ids:
                    continue
                current = latest.get(run.session_id)
                if current is None or (run.created_at, run.id.int) > (
                    current.created_at,
                    current.id.int,
                ):
                    latest[run.session_id] = run.model_copy(deep=True)
        return latest

    async def child_for_parent(
        self, parent_run_id: UUID, kind: RunKind, principal: Principal
    ) -> Run | None:
        async with self._lock:
            matching = [
                run.model_copy(deep=True)
                for run in self._runs.values()
                if run.parent_run_id == parent_run_id and run.kind is kind
            ]
        if not matching:
            return None
        if len(matching) > 1:
            raise ConflictError("parent has multiple child runs of one kind")
        await self._sessions.get(matching[0].session_id, principal)
        return matching[0]

    async def request_cancellation(self, run_id: UUID, expected_status: RunStatus) -> Run:
        async with self._lock:
            try:
                current = self._runs[run_id]
            except KeyError as exc:
                raise NotFoundError("run not found") from exc
            if current.status is not expected_status:
                raise ConflictError(
                    f"cancellation request expected {expected_status.value}, "
                    f"found {current.status.value}"
                )
            updated = current.model_copy(
                update={
                    "cancel_requested_at": current.cancel_requested_at or self._clock.now(),
                    "updated_at": self._clock.now(),
                },
                deep=True,
            )
            self._runs[run_id] = updated
            return updated.model_copy(deep=True)

    async def transition(
        self,
        run_id: UUID,
        expected_status: RunStatus,
        new_status: RunStatus,
        *,
        failure: object | None = None,
        final_message: str | None = None,
        lease: WorkerLease | None = None,
    ) -> Run:
        if lease is not None:
            raise NotImplementedError("the in-memory repository does not support worker leases")
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

    async def update_counters(self, run: Run, *, lease: WorkerLease | None = None) -> None:
        if lease is not None:
            raise NotImplementedError("the in-memory repository does not support worker leases")
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

    async def set_seed_event_sequence(self, run_id: UUID, sequence: int) -> None:
        async with self._lock:
            try:
                current = self._runs[run_id]
            except KeyError as exc:
                raise NotFoundError("run not found") from exc
            if current.seed_event_sequence != 0:
                raise ConflictError("run seed sequence was already assigned")
            self._runs[run_id] = current.model_copy(
                update={"seed_event_sequence": sequence, "updated_at": self._clock.now()},
                deep=True,
            )

    async def set_provider_pin(self, run_id: UUID, pin: ProviderPin) -> None:
        typed = ProviderPin.model_validate(pin)
        async with self._lock:
            try:
                current = self._runs[run_id]
            except KeyError as exc:
                raise NotFoundError("run not found") from exc
            if current.provider_pin is not None and current.provider_pin != typed:
                raise ConflictError("run provider pin is immutable")
            self._runs[run_id] = current.model_copy(
                update={"provider_pin": typed, "updated_at": self._clock.now()},
                deep=True,
            )


class InMemoryEventRepository:
    def __init__(self, sessions: SessionRepository, clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock
        self._events: dict[UUID, list[EventEnvelope]] = defaultdict(list)
        self._derived: dict[str, EventEnvelope] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def append(self, event: NewEvent, *, lease: WorkerLease | None = None) -> EventEnvelope:
        if lease is not None:
            raise NotImplementedError("the in-memory repository does not support worker leases")
        occurred_at = self._clock.now()
        async with self._lock:
            if event.derivation_key is not None:
                existing = self._derived.get(event.derivation_key)
                if existing is not None:
                    return existing.model_copy(deep=True)
            stream = self._events[event.session_id]
            envelope = EventEnvelope(
                id=self._next_id,
                sequence=len(stream) + 1,
                created_at=occurred_at,
                **event.model_dump(),
            )
            self._next_id += 1
            stream.append(envelope)
            if event.derivation_key is not None:
                self._derived[event.derivation_key] = envelope
        touch = getattr(self._sessions, "touch", None)
        if touch is not None:
            await touch(event.session_id, occurred_at)
        return envelope.model_copy(deep=True)

    async def list_after(
        self,
        session_id: UUID,
        sequence: int,
        principal: Principal,
        *,
        created_at_or_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be nonnegative")
        await self._sessions.get(session_id, principal)
        async with self._lock:
            matching = [
                event.model_copy(deep=True)
                for event in self._events[session_id]
                if event.sequence > sequence
                and (created_at_or_after is None or event.created_at >= created_at_or_after)
                and (created_before is None or event.created_at < created_before)
            ]
            return matching if limit is None else matching[:limit]

    async def latest_before(
        self,
        session_id: UUID,
        sequence: int,
        event_type: str,
        principal: Principal,
    ) -> EventEnvelope | None:
        await self._sessions.get(session_id, principal)
        async with self._lock:
            matching = [
                event
                for event in self._events[session_id]
                if event.sequence < sequence and event.event_type == event_type
            ]
            return None if not matching else matching[-1].model_copy(deep=True)

    async def existing_sequences(
        self,
        session_id: UUID,
        sequences: set[int],
        principal: Principal,
    ) -> set[int]:
        await self._sessions.get(session_id, principal)
        async with self._lock:
            return {
                event.sequence for event in self._events[session_id] if event.sequence in sequences
            }

    async def get_by_derivation(
        self, derivation_key: str, principal: Principal
    ) -> EventEnvelope | None:
        async with self._lock:
            event = self._derived.get(derivation_key)
            copied = None if event is None else event.model_copy(deep=True)
        if copied is None:
            return None
        await self._sessions.get(copied.session_id, principal)
        return copied

    async def raw_list(self, session_id: UUID, sequence: int = 0) -> list[EventEnvelope]:
        """Support the in-process projection without bypassing its lock."""

        async with self._lock:
            return [
                event.model_copy(deep=True)
                for event in self._events[session_id]
                if event.sequence > sequence
            ]

    async def raw_for_run(self, run_id: UUID) -> list[EventEnvelope]:
        async with self._lock:
            return sorted(
                (
                    event.model_copy(deep=True)
                    for stream in self._events.values()
                    for event in stream
                    if event.run_id == run_id
                ),
                key=lambda event: (event.sequence, event.id),
            )


class InMemoryProcessEventRepository:
    def __init__(self) -> None:
        self._events: dict[str, ProcessEvent] = {}
        self._lock = asyncio.Lock()

    async def append(self, event: ProcessEvent) -> ProcessEvent:
        async with self._lock:
            existing = self._events.get(event.derivation_key)
            if existing is not None:
                if existing.model_dump(exclude={"created_at"}) != event.model_dump(
                    exclude={"created_at"}
                ):
                    raise ConflictError("process event derivation identifies different content")
                return existing.model_copy(deep=True)
            self._events[event.derivation_key] = event.model_copy(deep=True)
            return event.model_copy(deep=True)

    async def list(self, event_type: str | None = None) -> list[ProcessEvent]:
        async with self._lock:
            rows = [
                event.model_copy(deep=True)
                for event in self._events.values()
                if event_type is None or event.event_type == event_type
            ]
        return sorted(rows, key=lambda event: (event.created_at, event.id.int))


class InMemoryToolInvocationRepository:
    def __init__(self, runs: RunRepository) -> None:
        self._runs = runs
        self._invocations: dict[UUID, ToolInvocation] = {}
        self._idempotency: dict[tuple[UUID, str], UUID] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, invocation: ToolInvocation, *, lease: WorkerLease | None = None
    ) -> ToolInvocation:
        if lease is not None:
            raise NotImplementedError("the in-memory repository does not support worker leases")
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
        *,
        lease: WorkerLease | None = None,
    ) -> ToolInvocation:
        if lease is not None:
            raise NotImplementedError("the in-memory repository does not support worker leases")
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
            if invocation.status not in ALLOWED_TOOL_TRANSITIONS[current.status]:
                raise ConflictError(
                    f"invalid tool transition {current.status.value}->{invocation.status.value}"
                )
            updated = current.model_copy(
                update={
                    "status": invocation.status,
                    "effect_sent_at": invocation.effect_sent_at,
                    "effective_arguments_hash": invocation.effective_arguments_hash,
                    "suspended_kind": invocation.suspended_kind,
                    "suspended_ref": invocation.suspended_ref,
                    "policy_decision": (
                        None
                        if invocation.policy_decision is None
                        else invocation.policy_decision.model_copy(deep=True)
                    ),
                    "structured_result": (
                        None
                        if invocation.structured_result is None
                        else dict(invocation.structured_result)
                    ),
                    "output_bytes": invocation.output_bytes,
                    "truncated": invocation.truncated,
                    "artifact_id": invocation.artifact_id,
                    "outcome": (
                        None
                        if invocation.outcome is None
                        else invocation.outcome.model_copy(deep=True)
                    ),
                    "result_item": (
                        None
                        if invocation.result_item is None
                        else invocation.result_item.model_copy(deep=True)
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


class InMemoryApprovalRepository:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._approvals: dict[UUID, ApprovalRequest] = {}
        self._actions: dict[UUID, UUID] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        async with self._lock:
            if request.id in self._approvals or request.action_id in self._actions:
                raise ConflictError("approval already exists for action")
            self._approvals[request.id] = request.model_copy(deep=True)
            self._actions[request.action_id] = request.id
            return request.model_copy(deep=True)

    async def discard_pending(self, approval_id: UUID) -> None:
        async with self._lock:
            request = self._approvals.get(approval_id)
            if request is None:
                return
            if request.status is not ApprovalStatus.PENDING:
                raise ConflictError("only a pending approval can be discarded")
            del self._approvals[approval_id]
            self._actions.pop(request.action_id, None)

    @staticmethod
    def _visible(request: ApprovalRequest, principal: Principal) -> bool:
        return request.tenant_id == principal.tenant_id

    async def get(self, approval_id: UUID, principal: Principal) -> ApprovalRequest:
        async with self._lock:
            request = self._approvals.get(approval_id)
            if request is None or not self._visible(request, principal):
                raise NotFoundError("approval not found")
            return request.model_copy(deep=True)

    async def get_by_action(self, action_id: UUID) -> ApprovalRequest | None:
        async with self._lock:
            approval_id = self._actions.get(action_id)
            if approval_id is None:
                return None
            return self._approvals[approval_id].model_copy(deep=True)

    async def record_revalidation(self, action_id: UUID, policy_version: str) -> ApprovalRequest:
        async with self._lock:
            approval_id = self._actions.get(action_id)
            if approval_id is None:
                raise NotFoundError("approval not found")
            request = self._approvals[approval_id]
            updated = request.model_copy(
                update={"revalidated_policy_version": policy_version}, deep=True
            )
            self._approvals[approval_id] = updated
            return updated.model_copy(deep=True)

    async def list_pending(
        self,
        principal: Principal,
        run_id: UUID | None = None,
        session_id: UUID | None = None,
        limit: int = 50,
        cursor: ApprovalCursor | None = None,
    ) -> list[ApprovalRequest]:
        async with self._lock:
            rows = [
                request.model_copy(deep=True)
                for request in self._approvals.values()
                if request.status is ApprovalStatus.PENDING
                and self._visible(request, principal)
                and (run_id is None or request.run_id == run_id)
                and (session_id is None or request.session_id == session_id)
                and (
                    cursor is None
                    or (request.created_at, request.id.int) < (cursor.created_at, cursor.id.int)
                )
            ]
        return sorted(rows, key=lambda row: (row.created_at, row.id.int), reverse=True)[:limit]

    async def resolve(
        self,
        approval_id: UUID,
        principal: Principal,
        resolution: ApprovalResolutionType,
        reason: str | None,
    ) -> ApprovalResolutionOutcome:
        async with self._lock:
            request = self._approvals.get(approval_id)
            if request is None or not self._visible(request, principal):
                raise NotFoundError("approval not found")
            if request.status is not ApprovalStatus.PENDING:
                state = (
                    ApprovalResolutionState.ALREADY_RESOLVED_IDENTICALLY
                    if request.resolution is resolution
                    else ApprovalResolutionState.ALREADY_RESOLVED_DIFFERENTLY
                )
                return ApprovalResolutionOutcome(
                    state=state, approval=request.model_copy(deep=True)
                )
            status = (
                ApprovalStatus.APPROVED
                if resolution is ApprovalResolutionType.APPROVE_ONCE
                else ApprovalStatus.DENIED
            )
            updated = request.model_copy(
                update={
                    "status": status,
                    "resolution": resolution,
                    "resolution_reason": reason,
                    "resolved_at": self._clock.now(),
                    "resolved_by": principal.principal_id,
                },
                deep=True,
            )
            self._approvals[approval_id] = updated
            return ApprovalResolutionOutcome(
                state=ApprovalResolutionState.APPLIED,
                approval=updated.model_copy(deep=True),
            )

    async def expire_due(
        self, now: datetime, limit: int, *, tenant_id: str
    ) -> list[ApprovalRequest]:
        async with self._lock:
            due = sorted(
                (
                    request
                    for request in self._approvals.values()
                    if request.status is ApprovalStatus.PENDING
                    and request.tenant_id == tenant_id
                    and request.expires_at is not None
                    and request.expires_at <= now
                ),
                key=lambda row: (row.expires_at or row.created_at, row.id.int),
            )[:limit]
            result: list[ApprovalRequest] = []
            for request in due:
                updated = request.model_copy(
                    update={"status": ApprovalStatus.EXPIRED, "resolved_at": now}, deep=True
                )
                self._approvals[request.id] = updated
                result.append(updated.model_copy(deep=True))
            return result

    async def cancel_for_run(self, run_id: UUID) -> int:
        async with self._lock:
            count = 0
            for approval_id, request in tuple(self._approvals.items()):
                if request.run_id == run_id and request.status is ApprovalStatus.PENDING:
                    self._approvals[approval_id] = request.model_copy(
                        update={
                            "status": ApprovalStatus.CANCELLED,
                            "resolved_at": self._clock.now(),
                        },
                        deep=True,
                    )
                    count += 1
            return count


class InMemoryPolicyProfileRepository:
    def __init__(self) -> None:
        self._profiles: dict[str, PolicyProfileRecord] = {}
        self._lock = asyncio.Lock()

    async def record(self, profile: PolicyProfileRecord) -> PolicyProfileRecord:
        async with self._lock:
            existing = self._profiles.get(profile.policy_version)
            if existing is not None and existing != profile:
                immutable = {"loaded_at", "loaded_by"}
                if existing.model_dump(exclude=immutable) != profile.model_dump(exclude=immutable):
                    raise ConflictError("policy version identifies different rules")
                return existing.model_copy(deep=True)
            self._profiles[profile.policy_version] = profile.model_copy(deep=True)
            return profile.model_copy(deep=True)

    async def get(self, policy_version: str) -> PolicyProfileRecord | None:
        async with self._lock:
            profile = self._profiles.get(policy_version)
            return None if profile is None else profile.model_copy(deep=True)


class InMemoryCheckpointRepository:
    def __init__(self) -> None:
        self._checkpoints: dict[UUID, list[tuple[RunCheckpoint, bool]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def write(
        self,
        run_id: UUID,
        checkpoint: RunCheckpoint,
        *,
        full: bool,
        lease: WorkerLease | None = None,
    ) -> int:
        if lease is not None:
            raise NotImplementedError("the in-memory repository does not support worker leases")
        async with self._lock:
            rows = self._checkpoints[run_id]
            expected = rows[-1][0].version + 1 if rows else 1
            if checkpoint.version != expected:
                raise ConflictError(f"checkpoint version must be {expected}")
            rows.append((checkpoint.model_copy(deep=True), full))
            return checkpoint.version

    async def latest(self, run_id: UUID) -> RunCheckpoint | None:
        async with self._lock:
            rows = self._checkpoints[run_id]
            return None if not rows else rows[-1][0].model_copy(deep=True)

    async def prune(self, run_id: UUID, *, terminal: bool) -> int:
        async with self._lock:
            rows = self._checkpoints[run_id]
            if len(rows) <= 1:
                return 0
            latest_full = next(
                (index for index in range(len(rows) - 1, -1, -1) if rows[index][1]),
                None,
            )
            if latest_full is None:
                raise ConflictError("checkpoint chain has no full snapshot")
            if terminal:
                checkpoint, full = rows[-1]
                if not full or checkpoint.status not in TERMINAL_RUN_STATUSES:
                    raise ConflictError(
                        "terminal checkpoint retention requires a final full snapshot"
                    )
                keep_from = len(rows) - 1
            else:
                keep_from = latest_full
            removed = keep_from
            self._checkpoints[run_id] = rows[keep_from:]
            return removed

    async def delete_nonterminal(self, run_id: UUID) -> int:
        async with self._lock:
            rows = self._checkpoints[run_id]
            if rows and rows[-1][0].status in TERMINAL_RUN_STATUSES:
                raise ConflictError("delete_nonterminal refuses a terminal run")
            rows = self._checkpoints.pop(run_id, [])
            return len(rows)


class InMemoryIdempotencyRepository:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._records: dict[tuple[str, str, str], IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, tenant_id: str, principal_id: str) -> IdempotencyRecord | None:
        async with self._lock:
            scoped_key = (tenant_id, principal_id, key)
            record = self._records.get(scoped_key)
            if record is None or record.expires_at <= self._clock.now():
                return None
            return record.model_copy(deep=True)

    async def create(self, record: IdempotencyRecord) -> IdempotencyRecord:
        async with self._lock:
            scoped_key = (record.tenant_id, record.principal_id, record.key)
            existing = self._records.get(scoped_key)
            if existing is not None and existing.expires_at <= self._clock.now():
                del self._records[scoped_key]
                existing = None
            if existing is not None:
                if existing.request_hash != record.request_hash:
                    raise ConflictError("idempotency key was reused with a different request")
                return existing.model_copy(deep=True)
            self._records[scoped_key] = record.model_copy(deep=True)
            return record.model_copy(deep=True)


class InMemoryUsageRepository:
    def __init__(self, runs: InMemoryRunRepository) -> None:
        self._runs = runs
        self._calls: dict[UUID, ModelCallRecord] = {}
        self._lock = asyncio.Lock()

    async def record_attempt(self, call: ModelCallRecord) -> None:
        async with self._lock:
            self._calls.setdefault(call.attempt_id, call.model_copy(deep=True))

    async def run_usage(self, run_id: UUID) -> RunUsage:
        async with self._lock:
            calls = [call for call in self._calls.values() if call.run_id == run_id]
            reasoning = [
                call.usage.reasoning_tokens
                for call in calls
                if call.usage.reasoning_tokens is not None
            ]
            return RunUsage(
                input_tokens=sum(call.usage.input_tokens for call in calls),
                cached_input_tokens=sum(call.usage.cached_input_tokens for call in calls),
                cache_write_input_tokens=sum(call.usage.cache_write_input_tokens for call in calls),
                output_tokens=sum(call.usage.output_tokens for call in calls),
                reasoning_tokens=sum(reasoning) if reasoning else None,
                model_calls=len(calls),
                cost=sum((call.cost for call in calls), Decimal("0")),
            )

    async def tenant_usage(
        self, tenant_id: str, *, since: datetime, until: datetime
    ) -> UsageRollup:
        async with self._lock:
            calls = [
                call
                for call in self._calls.values()
                if call.tenant_id == tenant_id and since <= call.started_at < until
            ]
            reasoning = [
                call.usage.reasoning_tokens
                for call in calls
                if call.usage.reasoning_tokens is not None
            ]
            return UsageRollup(
                input_tokens=sum(call.usage.input_tokens for call in calls),
                cached_input_tokens=sum(call.usage.cached_input_tokens for call in calls),
                cache_write_input_tokens=sum(call.usage.cache_write_input_tokens for call in calls),
                output_tokens=sum(call.usage.output_tokens for call in calls),
                reasoning_tokens=sum(reasoning) if reasoning else None,
                cost=sum((call.cost for call in calls), Decimal("0")),
            )


class InMemorySessionHistoryRepository:
    def __init__(self, events: InMemoryEventRepository) -> None:
        self._events = events

    @staticmethod
    def _project(session_id: UUID, events: list[EventEnvelope]) -> SessionHistory:
        items = [item for event in events for item in conversation_items(event)]
        return SessionHistory(
            session_id=session_id,
            through_sequence=events[-1].sequence if events else 0,
            items=items,
            builder_version="session-history-memory@2",
        )

    async def catch_up(self, session_id: UUID) -> SessionHistory:
        return self._project(session_id, await self._events.raw_list(session_id))

    async def rebuild(self, session_id: UUID) -> SessionHistory:
        return await self.catch_up(session_id)

    async def read(self, session_id: UUID, through_sequence: int | None = None) -> SessionHistory:
        events = await self._events.raw_list(session_id)
        history = self._project(session_id, events)
        if through_sequence is None or through_sequence >= history.through_sequence:
            return history
        truncated = self._project(
            session_id, [event for event in events if event.sequence <= through_sequence]
        )
        return truncated.model_copy(update={"through_sequence": through_sequence})


class InMemoryTrajectoryProjectionRepository:
    def __init__(self, events: InMemoryEventRepository) -> None:
        self._events = events

    async def catch_up(self, run_id: UUID) -> TrajectoryProjection | None:
        events = await self._events.raw_for_run(run_id)
        if not events:
            return None
        return TrajectoryProjection(
            run_id=run_id,
            first_sequence=events[0].sequence,
            last_sequence=events[-1].sequence,
            terminal=any(
                event.event_type
                in {f"run.{status.value.lower()}" for status in TERMINAL_RUN_STATUSES}
                for event in events
            ),
            builder_version="trajectory@1",
            updated_at=events[-1].created_at,
        )

    async def rebuild(self, run_id: UUID) -> TrajectoryProjection | None:
        return await self.catch_up(run_id)

    async def read(self, run_id: UUID) -> TrajectoryProjection | None:
        return await self.catch_up(run_id)


class InMemoryExportConsentRepository:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ExportConsent] = {}
        self._lock = asyncio.Lock()

    async def get(self, tenant_id: str, principal_id: str) -> ExportConsent | None:
        async with self._lock:
            row = self._rows.get((tenant_id, principal_id))
            return None if row is None else row.model_copy(deep=True)

    async def get_for_update(self, tenant_id: str, principal_id: str) -> ExportConsent | None:
        return await self.get(tenant_id, principal_id)

    async def grant(self, consent: ExportConsent) -> ExportConsent:
        async with self._lock:
            key = (consent.tenant_id, consent.principal_id)
            existing = self._rows.get(key)
            if existing is not None and existing.active:
                return existing.model_copy(deep=True)
            self._rows[key] = consent.model_copy(deep=True)
            return consent.model_copy(deep=True)

    async def withdraw(
        self, tenant_id: str, principal_id: str, withdrawn_at: datetime
    ) -> ExportConsent:
        async with self._lock:
            key = (tenant_id, principal_id)
            existing = self._rows.get(key)
            if existing is None:
                raise NotFoundError("export consent not found")
            if existing.withdrawn_at is not None:
                return existing.model_copy(deep=True)
            updated = existing.model_copy(update={"withdrawn_at": withdrawn_at})
            self._rows[key] = updated
            return updated.model_copy(deep=True)


class InMemoryTrajectoryExportRepository:
    def __init__(self) -> None:
        self._rows: dict[UUID, TrajectoryExport] = {}
        self._lock = asyncio.Lock()

    async def get_for_run(self, run_id: UUID) -> TrajectoryExport | None:
        async with self._lock:
            row = self._rows.get(run_id)
            return None if row is None else row.model_copy(deep=True)

    async def create(self, export: TrajectoryExport) -> TrajectoryExport:
        async with self._lock:
            existing = self._rows.get(export.run_id)
            if existing is not None:
                return existing.model_copy(deep=True)
            self._rows[export.run_id] = export.model_copy(deep=True)
            return export.model_copy(deep=True)

    async def get_artifact(self, artifact_id: UUID, principal: Principal) -> ArtifactRef:
        async with self._lock:
            for row in self._rows.values():
                artifact = row.artifact
                if (
                    artifact.id == artifact_id
                    and artifact.tenant_id == principal.tenant_id
                    and artifact.principal_id == principal.principal_id
                ):
                    return artifact.model_copy(deep=True)
        raise NotFoundError("artifact not found")

    async def expire_for_principal(
        self, tenant_id: str, principal_id: str, expired_at: datetime
    ) -> int:
        changed = 0
        async with self._lock:
            for run_id, row in list(self._rows.items()):
                if row.tenant_id != tenant_id or row.principal_id != principal_id:
                    continue
                if row.artifact.expires_at is None or row.artifact.expires_at <= expired_at:
                    continue
                artifact = row.artifact.model_copy(update={"expires_at": expired_at})
                self._rows[run_id] = row.model_copy(update={"artifact": artifact})
                changed += 1
        return changed

    async def list_expired(self, now: datetime, *, limit: int) -> list[ArtifactRef]:
        expired: list[ArtifactRef] = []
        async with self._lock:
            for row in self._rows.values():
                if len(expired) >= limit:
                    break
                if row.artifact.expires_at is not None and row.artifact.expires_at <= now:
                    expired.append(row.artifact.model_copy(deep=True))
        return expired

    async def delete_expired(self, artifact_id: UUID, *, now: datetime) -> bool:
        async with self._lock:
            for run_id, row in list(self._rows.items()):
                if (
                    row.artifact.id == artifact_id
                    and row.artifact.expires_at is not None
                    and row.artifact.expires_at <= now
                ):
                    del self._rows[run_id]
                    return True
        return False


class InMemoryArtifactRepository:
    def __init__(self) -> None:
        self._rows: dict[UUID, ArtifactRef] = {}
        self._lock = asyncio.Lock()

    async def create(self, artifact: ArtifactRef) -> ArtifactRef:
        async with self._lock:
            existing = self._rows.get(artifact.id)
            if existing is not None and existing != artifact:
                raise ConflictError("artifact id already exists with different metadata")
            self._rows[artifact.id] = artifact.model_copy(deep=True)
            return artifact.model_copy(deep=True)

    async def exists(self, artifact_id: UUID) -> bool:
        async with self._lock:
            return artifact_id in self._rows

    async def get(self, artifact_id: UUID, principal: Principal) -> ArtifactRef:
        async with self._lock:
            artifact = self._rows.get(artifact_id)
            if artifact is not None and (
                artifact.tenant_id == principal.tenant_id
                and artifact.principal_id == principal.principal_id
            ):
                return artifact.model_copy(deep=True)
        raise NotFoundError("artifact not found")

    async def retain_for_knowledge(self, artifact_id: UUID, principal: Principal) -> ArtifactRef:
        async with self._lock:
            artifact = self._rows.get(artifact_id)
            if artifact is None or (
                artifact.tenant_id != principal.tenant_id
                or artifact.principal_id != principal.principal_id
            ):
                raise NotFoundError("artifact not found")
            retained = artifact.model_copy(
                update={"origin": "knowledge_source", "expires_at": None}, deep=True
            )
            self._rows[artifact_id] = retained
            return retained.model_copy(deep=True)

    async def expire(
        self, artifact_id: UUID, principal: Principal, expired_at: datetime
    ) -> ArtifactRef:
        async with self._lock:
            artifact = self._rows.get(artifact_id)
            if artifact is None or (
                artifact.tenant_id != principal.tenant_id
                or artifact.principal_id != principal.principal_id
            ):
                raise NotFoundError("artifact not found")
            expired = artifact.model_copy(update={"expires_at": expired_at}, deep=True)
            self._rows[artifact_id] = expired
            return expired.model_copy(deep=True)

    async def list_expired(self, now: datetime, *, limit: int) -> list[ArtifactRef]:
        async with self._lock:
            expired = [
                artifact
                for artifact in self._rows.values()
                if artifact.origin != "trajectory_export"
                and artifact.expires_at is not None
                and artifact.expires_at <= now
            ]
            return [
                artifact.model_copy(deep=True)
                for artifact in sorted(
                    expired,
                    key=lambda item: (item.expires_at, item.id),
                )
            ][:limit]

    async def delete_expired(self, artifact_id: UUID, *, now: datetime) -> bool:
        async with self._lock:
            artifact = self._rows.get(artifact_id)
            if (
                artifact is None
                or artifact.origin == "trajectory_export"
                or artifact.expires_at is None
                or artifact.expires_at > now
            ):
                return False
            del self._rows[artifact_id]
            return True


class InMemoryMaintenanceRepository:
    async def live_run_leases(self) -> frozenset[tuple[UUID, int]]:
        return frozenset()

    async def is_live_run_lease(self, run_id: UUID, lease_epoch: int) -> bool:
        del run_id, lease_epoch
        return False

    async def projection_sessions(self, limit: int) -> list[UUID]:
        del limit
        return []

    async def checkpoint_runs(self, limit: int) -> list[tuple[UUID, bool]]:
        del limit
        return []

    async def trajectory_runs(self, limit: int) -> list[UUID]:
        del limit
        return []
