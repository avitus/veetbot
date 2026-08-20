"""PostgreSQL repositories constructed over one live async session."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import DateTime, Text, and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import cast as sql_cast

from agent_core.adapters.live_events import event_channel
from agent_core.adapters.persistence.mappers import (
    agent_to_domain,
    agent_values,
    approval_to_domain,
    approval_values,
    artifact_to_domain,
    artifact_values,
    event_to_domain,
    event_values,
    idempotency_to_domain,
    idempotency_values,
    invocation_to_domain,
    invocation_values,
    model_call_values,
    run_to_domain,
    run_values,
    session_to_domain,
    session_values,
    trajectory_export_to_domain,
    trajectory_export_values,
)
from agent_core.adapters.persistence.sqlalchemy_models import (
    AgentRow,
    ApprovalRow,
    ArtifactRow,
    BrowserAuthenticationRow,
    BrowserGrantRow,
    BrowserProfileRow,
    CheckpointRow,
    ConsolidationWatermarkRow,
    DerivedEventKeyRow,
    EvalCriterionScoreRow,
    EvalScenarioAttemptCostRow,
    EvalScenarioRunRow,
    EventRow,
    ExportConsentRow,
    IdempotencyKeyRow,
    ModelCallRow,
    PolicyProfileRow,
    ProcessEventRow,
    ProjectionWatermarkRow,
    RunRow,
    SessionRow,
    ToolInvocationRow,
    TrajectoryExportRow,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import (
    ApprovalCursor,
    ApprovalRequest,
    ApprovalResolutionOutcome,
    ApprovalResolutionState,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.browser import (
    ALLOWED_BROWSER_AUTHENTICATION_TRANSITIONS,
    ALLOWED_BROWSER_PROFILE_TRANSITIONS,
    BrowserActionKind,
    BrowserAuthenticationRecord,
    BrowserAuthenticationStatus,
    BrowserGrant,
    BrowserProfile,
    BrowserProfileProvisioning,
    BrowserProfileStatus,
)
from agent_core.domain.errors import (
    ConcurrencyConflict,
    ConflictError,
    NotFoundError,
    WorkerFencedError,
)
from agent_core.domain.evaluations import EvalCriterionScore, EvalScenarioRun, SavedEvalScenario
from agent_core.domain.events import (
    CONVERSATION_MESSAGE_EVENTS,
    EventEnvelope,
    NewEvent,
    ProcessEvent,
)
from agent_core.domain.messages import ProviderPin
from agent_core.domain.persistence import (
    IdempotencyRecord,
    ModelCallRecord,
    SessionHistory,
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
from agent_core.runtime.state_machine import require_transition

ACTIVE_RUN_CONSTRAINT = "uq_runs_one_active_per_session"
PARENT_SKILL_REVIEW_CONSTRAINT = "uq_runs_parent_skill_review"
SESSION_HISTORY_PROJECTION = "session_history"
TRAJECTORY_PROJECTION = "trajectory_export"


def _rowcount(result: Any) -> int:
    """Return affected rows from an async DML result."""

    return int(result.rowcount or 0)


def _constraint_name(exc: IntegrityError) -> str | None:
    candidates = (exc.orig, getattr(exc.orig, "__cause__", None))
    for candidate in candidates:
        if candidate is None:
            continue
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
        diagnostic = getattr(candidate, "diag", None)
        name = getattr(diagnostic, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


async def execute_run_insert(session: AsyncSession, statement: Any) -> int:
    """Insert a run while preserving the active-run conflict as a typed error."""

    try:
        async with session.begin_nested():
            return _rowcount(await session.execute(statement))
    except IntegrityError as exc:
        constraint = _constraint_name(exc)
        if constraint == ACTIVE_RUN_CONSTRAINT:
            raise ConflictError("session already has a non-terminal run") from exc
        if constraint == PARENT_SKILL_REVIEW_CONSTRAINT:
            raise ConflictError("parent run already has a skill review") from exc
        raise


class PostgresAgentRepository:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def put(self, agent: AgentSpec) -> None:
        statement = (
            pg_insert(AgentRow)
            .values(**agent_values(agent, created_at=self._clock.now()))
            .on_conflict_do_nothing(index_elements=[AgentRow.id, AgentRow.version])
        )
        inserted = _rowcount(await self._session.execute(statement))
        if inserted:
            return
        existing = await self.get_version(agent.id, agent.version)
        if existing != agent:
            raise ConflictError("agent version already exists with different content")

    async def get_version(self, agent_id: UUID, agent_version: str) -> AgentSpec:
        row = await self._session.get(AgentRow, (agent_id, agent_version))
        if row is None:
            raise NotFoundError("agent version not found")
        return agent_to_domain(row)

    async def latest_version(self, agent_id: UUID) -> AgentSpec:
        row = (
            await self._session.scalars(
                select(AgentRow)
                .where(AgentRow.id == agent_id)
                .order_by(AgentRow.created_at.desc(), AgentRow.version.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            raise NotFoundError("agent not found")
        return agent_to_domain(row)


class PostgresSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, session: Session) -> None:
        statement = pg_insert(SessionRow).values(**session_values(session)).on_conflict_do_nothing()
        if not _rowcount(await self._session.execute(statement)):
            raise ConflictError("session already exists")

    @staticmethod
    def _title_from_event_payload(payload: dict[str, Any]) -> str | None:
        content = payload.get("content")
        if isinstance(content, str):
            return conversation_title(content)
        if not isinstance(content, list):
            return None
        for part in content:
            if not isinstance(part, dict) or part.get("kind") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                title = conversation_title(text)
                if title is not None:
                    return title
        return None

    async def _with_legacy_titles(
        self, sessions: list[Session], principal: Principal
    ) -> list[Session]:
        missing_ids = [session.id for session in sessions if session.title is None]
        if not missing_ids:
            return sessions
        rows = (
            await self._session.execute(
                select(EventRow.session_id, EventRow.payload)
                .join(SessionRow, SessionRow.id == EventRow.session_id)
                .where(
                    EventRow.session_id.in_(missing_ids),
                    EventRow.event_type == "user.message.created",
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
                .distinct(EventRow.session_id)
                .order_by(EventRow.session_id, EventRow.sequence, EventRow.id)
            )
        ).all()
        titles = {
            session_id: title
            for session_id, payload in rows
            if (title := self._title_from_event_payload(payload)) is not None
        }
        if titles:
            await self._session.execute(
                update(SessionRow)
                .where(
                    SessionRow.id.in_(titles),
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                    SessionRow.title.is_(None),
                )
                .values(title=case(titles, value=SessionRow.id))
            )
            stored_rows = (
                await self._session.execute(
                    select(SessionRow.id, SessionRow.title).where(
                        SessionRow.id.in_(titles),
                        SessionRow.tenant_id == principal.tenant_id,
                        SessionRow.principal_id == principal.principal_id,
                    )
                )
            ).all()
            titles = {session_id: title for session_id, title in stored_rows if title is not None}
        return [
            session.model_copy(update={"title": titles[session.id]})
            if session.id in titles
            else session
            for session in sessions
        ]

    async def get(self, session_id: UUID, principal: Principal) -> Session:
        row = (
            await self._session.scalars(
                select(SessionRow).where(
                    SessionRow.id == session_id,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("session not found")
        return (await self._with_legacy_titles([session_to_domain(row)], principal))[0]

    async def set_title_if_missing(
        self, session_id: UUID, principal: Principal, title: str
    ) -> Session:
        normalized = conversation_title(title)
        if normalized is None:
            raise ValueError("session title must contain text")
        row = (
            await self._session.scalars(
                update(SessionRow)
                .where(
                    SessionRow.id == session_id,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                    SessionRow.title.is_(None),
                )
                .values(title=normalized)
                .returning(SessionRow)
            )
        ).one_or_none()
        if row is not None:
            return session_to_domain(row)
        return await self.get(session_id, principal)

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: SessionCursor | None = None,
    ) -> list[Session]:
        predicates: list[Any] = [
            SessionRow.tenant_id == principal.tenant_id,
            SessionRow.principal_id == principal.principal_id,
        ]
        if cursor is not None:
            predicates.append(
                (SessionRow.updated_at < cursor.updated_at)
                | ((SessionRow.updated_at == cursor.updated_at) & (SessionRow.id < cursor.id))
            )
        rows = (
            await self._session.scalars(
                select(SessionRow)
                .where(*predicates)
                .order_by(SessionRow.updated_at.desc(), SessionRow.id.desc())
                .limit(limit)
            )
        ).all()
        return await self._with_legacy_titles([session_to_domain(row) for row in rows], principal)

    async def close(
        self, session_id: UUID, principal: Principal, closed_at: datetime
    ) -> tuple[Session, bool]:
        row = (
            await self._session.scalars(
                update(SessionRow)
                .where(
                    SessionRow.id == session_id,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                    SessionRow.status == SessionStatus.ACTIVE.value,
                )
                .values(status=SessionStatus.CLOSED.value, updated_at=closed_at)
                .returning(SessionRow)
            )
        ).one_or_none()
        if row is not None:
            return (await self._with_legacy_titles([session_to_domain(row)], principal))[0], True
        return await self.get(session_id, principal), False


def _lease_predicates(lease: WorkerLease | None) -> list[Any]:
    if lease is None:
        return []
    return [
        RunRow.lease_owner == lease.worker_id,
        RunRow.lease_epoch == lease.lease_epoch,
    ]


class PostgresRunRepository:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def create(self, run: Run) -> None:
        statement = (
            pg_insert(RunRow)
            .values(**run_values(run))
            .on_conflict_do_nothing(index_elements=[RunRow.id])
        )
        if not await execute_run_insert(self._session, statement):
            raise ConflictError("run already exists")

    async def get(self, run_id: UUID, principal: Principal) -> Run:
        row = (
            await self._session.scalars(
                select(RunRow)
                .join(SessionRow, SessionRow.id == RunRow.session_id)
                .where(
                    RunRow.id == run_id,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("run not found")
        return run_to_domain(row)

    async def active_for_session(self, session_id: UUID, principal: Principal) -> Run | None:
        rows = (
            await self._session.scalars(
                select(RunRow)
                .join(SessionRow, SessionRow.id == RunRow.session_id)
                .where(
                    RunRow.session_id == session_id,
                    RunRow.status.not_in([status.value for status in TERMINAL_RUN_STATUSES]),
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
                .limit(2)
            )
        ).all()
        if len(rows) > 1:
            raise ConflictError("session has multiple active runs")
        return None if not rows else run_to_domain(rows[0])

    async def latest_for_session(self, session_id: UUID, principal: Principal) -> Run | None:
        await PostgresSessionRepository(self._session).get(session_id, principal)
        row = (
            await self._session.scalars(
                select(RunRow)
                .where(RunRow.session_id == session_id)
                .order_by(RunRow.created_at.desc(), RunRow.id.desc())
                .limit(1)
            )
        ).one_or_none()
        return None if row is None else run_to_domain(row)

    async def latest_for_sessions(
        self, session_ids: list[UUID], principal: Principal
    ) -> dict[UUID, Run]:
        if not session_ids:
            return {}
        rows = (
            await self._session.scalars(
                select(RunRow)
                .join(SessionRow, SessionRow.id == RunRow.session_id)
                .where(
                    RunRow.session_id.in_(session_ids),
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
                .distinct(RunRow.session_id)
                .order_by(RunRow.session_id, RunRow.created_at.desc(), RunRow.id.desc())
            )
        ).all()
        return {row.session_id: run_to_domain(row) for row in rows}

    async def child_for_parent(
        self, parent_run_id: UUID, kind: RunKind, principal: Principal
    ) -> Run | None:
        rows = (
            await self._session.scalars(
                select(RunRow)
                .join(SessionRow, SessionRow.id == RunRow.session_id)
                .where(
                    RunRow.parent_run_id == parent_run_id,
                    RunRow.run_kind == kind.value,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
                .limit(2)
            )
        ).all()
        if len(rows) > 1:
            raise ConflictError("parent has multiple child runs of one kind")
        return None if not rows else run_to_domain(rows[0])

    async def request_cancellation(self, run_id: UUID, expected_status: RunStatus) -> Run:
        statement = (
            update(RunRow)
            .where(RunRow.id == run_id, RunRow.status == expected_status.value)
            .values(
                cancel_requested_at=func.coalesce(RunRow.cancel_requested_at, self._clock.now()),
                updated_at=self._clock.now(),
            )
            .returning(RunRow)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            await self._raise_guard_failure(run_id, expected_status, None, "cancellation request")
            raise AssertionError("guard failure helper must raise")
        return run_to_domain(row)

    async def _raise_guard_failure(
        self,
        run_id: UUID,
        expected_status: RunStatus,
        lease: WorkerLease | None,
        operation: str,
    ) -> None:
        row = await self._session.get(RunRow, run_id)
        if row is None:
            raise NotFoundError("run not found")
        if lease is not None and (
            row.lease_owner != lease.worker_id or row.lease_epoch != lease.lease_epoch
        ):
            raise WorkerFencedError(f"{operation} guard failed; worker was fenced")
        raise ConflictError(f"{operation} expected {expected_status.value}, found {row.status}")

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
        require_transition(expected_status, new_status)
        typed_failure = None if failure is None else RunFailure.model_validate(failure)
        assignments: dict[str, Any] = {
            "status": new_status.value,
            "updated_at": self._clock.now(),
        }
        if typed_failure is not None:
            assignments["failure"] = typed_failure.model_dump(mode="json")
        if final_message is not None:
            assignments["final_message"] = final_message
        statement = (
            update(RunRow)
            .where(
                RunRow.id == run_id,
                RunRow.status == expected_status.value,
                *_lease_predicates(lease),
            )
            .values(**assignments)
            .returning(RunRow)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            await self._raise_guard_failure(run_id, expected_status, lease, "run transition")
            raise AssertionError("guard failure helper must raise")
        return run_to_domain(row)

    async def update_counters(self, run: Run, *, lease: WorkerLease | None = None) -> None:
        statement = (
            update(RunRow)
            .where(
                RunRow.id == run.id,
                RunRow.status == run.status.value,
                *_lease_predicates(lease),
            )
            .values(
                step_count=run.step_count,
                model_call_count=run.model_call_count,
                tool_call_count=run.tool_call_count,
                usage=run.usage.model_dump(mode="json"),
                updated_at=run.updated_at,
            )
        )
        if not _rowcount(await self._session.execute(statement)):
            await self._raise_guard_failure(run.id, run.status, lease, "counter update")

    async def set_seed_event_sequence(self, run_id: UUID, sequence: int) -> None:
        statement = (
            update(RunRow)
            .where(RunRow.id == run_id, RunRow.seed_event_sequence == 0)
            .values(seed_event_sequence=sequence, updated_at=self._clock.now())
        )
        if not _rowcount(await self._session.execute(statement)):
            if await self._session.get(RunRow, run_id) is None:
                raise NotFoundError("run not found")
            raise ConflictError("run seed sequence was already assigned")

    async def set_provider_pin(self, run_id: UUID, pin: ProviderPin) -> None:
        typed = ProviderPin.model_validate(pin)
        serialized = typed.model_dump(mode="json")
        statement = (
            update(RunRow)
            .where(RunRow.id == run_id, RunRow.provider_pin.is_(None))
            .values(provider_pin=serialized, updated_at=self._clock.now())
        )
        if _rowcount(await self._session.execute(statement)):
            return
        existing = (
            await self._session.execute(
                select(RunRow.id, RunRow.provider_pin).where(RunRow.id == run_id)
            )
        ).one_or_none()
        if existing is None:
            raise NotFoundError("run not found")
        if existing.provider_pin != serialized:
            raise ConflictError("run provider pin is immutable")


class PostgresEventRepository:
    def __init__(
        self, session: AsyncSession, clock: Clock, upcasters: EventUpcasterRegistry
    ) -> None:
        self._session = session
        self._clock = clock
        self._upcasters = upcasters

    async def append(self, event: NewEvent, *, lease: WorkerLease | None = None) -> EventEnvelope:
        if lease is not None:
            if event.run_id is not None and event.run_id != lease.run_id:
                raise ConflictError("leased event run does not match the worker lease")
            guard = (
                update(RunRow)
                .where(RunRow.id == lease.run_id, *_lease_predicates(lease))
                .values(updated_at=RunRow.updated_at)
            )
            if not _rowcount(await self._session.execute(guard)):
                raise WorkerFencedError("event append guard failed; worker was fenced")
        if event.derivation_key is not None:
            await self._session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(event.derivation_key, 0)))
            )
            existing = (
                await self._session.scalars(
                    select(EventRow)
                    .join(DerivedEventKeyRow, DerivedEventKeyRow.event_id == EventRow.id)
                    .where(DerivedEventKeyRow.derivation_key == event.derivation_key)
                )
            ).one_or_none()
            if existing is not None:
                return event_to_domain(existing, self._upcasters)

        occurred_at = self._clock.now()
        allocation = (
            update(SessionRow)
            .where(SessionRow.id == event.session_id)
            .values(
                next_event_sequence=SessionRow.next_event_sequence + 1,
                updated_at=func.greatest(SessionRow.updated_at, occurred_at),
            )
            .returning(SessionRow.next_event_sequence - 1)
        )
        sequence = (await self._session.execute(allocation)).scalar_one_or_none()
        if sequence is None:
            raise NotFoundError("session not found")
        statement = (
            pg_insert(EventRow)
            .values(**event_values(event, sequence=sequence, created_at=occurred_at))
            .returning(EventRow)
        )
        row = (await self._session.scalars(statement)).one()
        if event.derivation_key is not None:
            await self._session.execute(
                pg_insert(DerivedEventKeyRow).values(
                    derivation_key=event.derivation_key,
                    event_id=row.id,
                    created_at=self._clock.now(),
                )
            )
        await self._session.execute(
            select(
                func.pg_notify(
                    event_channel(event.session_id),
                    f'{{"kind":"persisted","sequence":{sequence}}}',
                )
            )
        )
        return event_to_domain(row, self._upcasters)

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
        allowed = await self._session.scalar(
            select(SessionRow.id).where(
                SessionRow.id == session_id,
                SessionRow.tenant_id == principal.tenant_id,
                SessionRow.principal_id == principal.principal_id,
            )
        )
        if allowed is None:
            raise NotFoundError("session not found")
        statement = select(EventRow).where(
            EventRow.session_id == session_id, EventRow.sequence > sequence
        )
        if created_at_or_after is not None:
            statement = statement.where(EventRow.created_at >= created_at_or_after)
        if created_before is not None:
            statement = statement.where(EventRow.created_at < created_before)
        statement = statement.order_by(EventRow.sequence, EventRow.id)
        if limit is not None:
            statement = statement.limit(limit)
        rows = (await self._session.scalars(statement)).all()
        return [event_to_domain(row, self._upcasters) for row in rows]

    async def latest_before(
        self,
        session_id: UUID,
        sequence: int,
        event_type: str,
        principal: Principal,
    ) -> EventEnvelope | None:
        row = (
            await self._session.scalars(
                select(EventRow)
                .join(SessionRow, SessionRow.id == EventRow.session_id)
                .where(
                    EventRow.session_id == session_id,
                    EventRow.sequence < sequence,
                    EventRow.event_type == event_type,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
                .order_by(EventRow.sequence.desc(), EventRow.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            allowed = await self._session.scalar(
                select(SessionRow.id).where(
                    SessionRow.id == session_id,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
            )
            if allowed is None:
                raise NotFoundError("session not found")
            return None
        return event_to_domain(row, self._upcasters)

    async def list_conversation_after(
        self,
        session_id: UUID,
        sequence: int,
        principal: Principal,
        *,
        limit: int,
    ) -> list[EventEnvelope]:
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        allowed = await self._session.scalar(
            select(SessionRow.id).where(
                SessionRow.id == session_id,
                SessionRow.tenant_id == principal.tenant_id,
                SessionRow.principal_id == principal.principal_id,
            )
        )
        if allowed is None:
            raise NotFoundError("session not found")
        rows = (
            await self._session.scalars(
                select(EventRow)
                .where(
                    EventRow.session_id == session_id,
                    EventRow.sequence > sequence,
                    EventRow.event_type.in_(CONVERSATION_MESSAGE_EVENTS),
                )
                .order_by(EventRow.sequence, EventRow.id)
                .limit(limit)
            )
        ).all()
        return [event_to_domain(row, self._upcasters) for row in rows]

    async def existing_sequences(
        self,
        session_id: UUID,
        sequences: set[int],
        principal: Principal,
    ) -> set[int]:
        rows = (
            await self._session.scalars(
                select(EventRow.sequence)
                .join(SessionRow, SessionRow.id == EventRow.session_id)
                .where(
                    EventRow.session_id == session_id,
                    EventRow.sequence.in_(sequences),
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
            )
        ).all()
        if rows or sequences:
            allowed = await self._session.scalar(
                select(SessionRow.id).where(
                    SessionRow.id == session_id,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
            )
            if allowed is None:
                raise NotFoundError("session not found")
        return set(rows)

    async def get_by_derivation(
        self, derivation_key: str, principal: Principal
    ) -> EventEnvelope | None:
        row = (
            await self._session.scalars(
                select(EventRow)
                .join(DerivedEventKeyRow, DerivedEventKeyRow.event_id == EventRow.id)
                .join(SessionRow, SessionRow.id == EventRow.session_id)
                .where(
                    DerivedEventKeyRow.derivation_key == derivation_key,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        return None if row is None else event_to_domain(row, self._upcasters)


def _checkpoint_delta(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in previous.keys() - current.keys():
        changes[key] = {"$remove": True}
    for key, value in current.items():
        old = previous.get(key)
        if old == value:
            continue
        if isinstance(old, list) and isinstance(value, list) and value[: len(old)] == old:
            changes[key] = {"$append": value[len(old) :]}
        else:
            changes[key] = {"$replace": value}
    return {"kind": "delta", "changes": changes}


def _apply_checkpoint_delta(state: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    result = dict(state)
    changes = delta.get("changes")
    if not isinstance(changes, dict):
        raise ConflictError("checkpoint delta has no changes mapping")
    for key, operation in changes.items():
        if not isinstance(operation, dict):
            raise ConflictError("checkpoint delta operation is malformed")
        if "$remove" in operation:
            result.pop(key, None)
        elif "$append" in operation:
            prior = result.get(key, [])
            if not isinstance(prior, list) or not isinstance(operation["$append"], list):
                raise ConflictError("checkpoint append delta is malformed")
            result[key] = [*prior, *operation["$append"]]
        elif "$replace" in operation:
            result[key] = operation["$replace"]
        else:
            raise ConflictError("checkpoint delta operation is unknown")
    return result


class _CheckpointHistory(Protocol):
    async def catch_up(self, session_id: UUID) -> SessionHistory: ...

    async def read(
        self, session_id: UUID, through_sequence: int | None = None
    ) -> SessionHistory: ...


class PostgresCheckpointRepository:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        history: _CheckpointHistory,
    ) -> None:
        self._session = session
        self._clock = clock
        self._history = history

    async def _rows(self, run_id: UUID) -> list[CheckpointRow]:
        return list(
            (
                await self._session.scalars(
                    select(CheckpointRow)
                    .where(CheckpointRow.run_id == run_id)
                    .order_by(CheckpointRow.version)
                )
            ).all()
        )

    @staticmethod
    def _stored_state(rows: list[CheckpointRow]) -> dict[str, Any] | None:
        if not rows:
            return None
        state: dict[str, Any] | None = None
        for row in rows:
            if row.full:
                value = row.state.get("value")
                if not isinstance(value, dict):
                    raise ConflictError("full checkpoint has no value mapping")
                state = dict(value)
            else:
                if state is None:
                    raise ConflictError("checkpoint delta chain has no full base")
                state = _apply_checkpoint_delta(state, row.state)
        if state is None:
            raise ConflictError("checkpoint chain has no full snapshot")
        return state

    async def _materialize(self, rows: list[CheckpointRow]) -> RunCheckpoint | None:
        state = self._stored_state(rows)
        if state is None:
            return None
        reference = state.get("conversation")
        if not isinstance(reference, dict) or not isinstance(
            (history_ref := reference.get("$session_history")), dict
        ):
            raise ConflictError("checkpoint conversation is not an event-history reference")
        try:
            session_id = UUID(str(history_ref["session_id"]))
            through_sequence = int(history_ref["through_sequence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConflictError("checkpoint conversation reference is malformed") from exc
        await self._history.catch_up(session_id)
        history = await self._history.read(session_id, through_sequence)
        if history.through_sequence < through_sequence:
            raise ConflictError("session-history projection did not reach the checkpoint sequence")
        state["conversation"] = [item.model_dump(mode="json") for item in history.items]
        return RunCheckpoint.model_validate(state)

    async def write(
        self,
        run_id: UUID,
        checkpoint: RunCheckpoint,
        *,
        full: bool,
        lease: WorkerLease | None = None,
    ) -> int:
        if run_id != checkpoint.run_id:
            raise ConflictError("checkpoint run identity cannot change")
        if lease is not None:
            guard = (
                update(RunRow)
                .where(RunRow.id == run_id, *_lease_predicates(lease))
                .values(updated_at=RunRow.updated_at)
            )
            if not _rowcount(await self._session.execute(guard)):
                raise WorkerFencedError("checkpoint write guard failed; worker was fenced")
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"checkpoint:{run_id}", 0)))
        )
        rows = await self._rows(run_id)
        previous = self._stored_state(rows)
        expected_version = 1 if not rows else rows[-1].version + 1
        if checkpoint.version != expected_version:
            raise ConflictError(
                f"checkpoint version {checkpoint.version} does not follow {expected_version - 1}"
            )
        effective_full = full or previous is None
        state = checkpoint.model_dump(mode="json")
        run_row = await self._session.get(RunRow, run_id)
        if run_row is None:
            raise NotFoundError("run not found")
        state["conversation"] = {
            "$session_history": {
                "session_id": str(run_row.session_id),
                "through_sequence": checkpoint.last_event_sequence,
            }
        }
        stored = {"kind": "full", "value": state}
        base_version: int | None = None
        if not effective_full and previous is not None:
            stored = _checkpoint_delta(previous, state)
            base_version = next(
                (row.version for row in reversed(rows) if row.full),
                None,
            )
            if base_version is None:
                raise ConflictError("checkpoint chain has no full snapshot")
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(CheckpointRow).values(
                        run_id=run_id,
                        version=checkpoint.version,
                        state=stored,
                        last_event_sequence=checkpoint.last_event_sequence,
                        full=effective_full,
                        base_version=base_version,
                        created_at=self._clock.now(),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(
                f"checkpoint version {checkpoint.version} was written concurrently"
            ) from exc
        return checkpoint.version

    async def latest(self, run_id: UUID) -> RunCheckpoint | None:
        return await self._materialize(await self._rows(run_id))

    async def prune(self, run_id: UUID, *, terminal: bool) -> int:
        rows = await self._rows(run_id)
        if len(rows) <= 1:
            return 0
        latest_full = next((row.version for row in reversed(rows) if row.full), None)
        if latest_full is None:
            raise ConflictError("checkpoint chain has no full snapshot")
        if terminal:
            if not rows[-1].full:
                raise ConflictError("terminal checkpoint retention requires a final full snapshot")
            keep = {rows[-1].version}
        else:
            keep = {row.version for row in rows if row.version >= latest_full}
        result = await self._session.execute(
            delete(CheckpointRow).where(
                CheckpointRow.run_id == run_id, CheckpointRow.version.not_in(keep)
            )
        )
        return _rowcount(result)

    async def delete_nonterminal(self, run_id: UUID) -> int:
        rows = await self._rows(run_id)
        if not rows:
            return 0
        run_row = await self._session.get(RunRow, run_id)
        if run_row is None:
            raise NotFoundError("run not found")
        terminal = RunStatus(run_row.status) in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
        if terminal:
            raise ConflictError("delete_nonterminal refuses a terminal run")
        removable = [row.version for row in rows]
        if not removable:
            return 0
        result = await self._session.execute(
            delete(CheckpointRow).where(
                CheckpointRow.run_id == run_id, CheckpointRow.version.in_(removable)
            )
        )
        return _rowcount(result)


class PostgresToolInvocationRepository:
    def __init__(self, session: AsyncSession, runs: PostgresRunRepository) -> None:
        self._session = session
        self._runs = runs

    async def _guard_lease(self, lease: WorkerLease | None) -> None:
        if lease is None:
            return
        statement = (
            update(RunRow)
            .where(RunRow.id == lease.run_id, *_lease_predicates(lease))
            .values(updated_at=RunRow.updated_at)
        )
        if not _rowcount(await self._session.execute(statement)):
            raise WorkerFencedError("tool invocation guard failed; worker was fenced")

    async def create(
        self, invocation: ToolInvocation, *, lease: WorkerLease | None = None
    ) -> ToolInvocation:
        await self._guard_lease(lease)
        statement = (
            pg_insert(ToolInvocationRow)
            .values(**invocation_values(invocation))
            .on_conflict_do_nothing(index_elements=[ToolInvocationRow.idempotency_key])
            .returning(ToolInvocationRow)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            existing = await self.find_by_idempotency_key(
                invocation.run_id, invocation.idempotency_key
            )
            if existing is None:
                raise ConflictError("tool idempotency key already belongs to another run")
            return existing
        return invocation_to_domain(row)

    async def find_by_idempotency_key(
        self, run_id: UUID, idempotency_key: str
    ) -> ToolInvocation | None:
        row = (
            await self._session.scalars(
                select(ToolInvocationRow).where(
                    ToolInvocationRow.run_id == run_id,
                    ToolInvocationRow.idempotency_key == idempotency_key,
                )
            )
        ).one_or_none()
        return None if row is None else invocation_to_domain(row)

    async def transition(
        self,
        invocation_id: UUID,
        expected_status: ToolInvocationStatus,
        invocation: ToolInvocation,
        *,
        lease: WorkerLease | None = None,
    ) -> ToolInvocation:
        await self._guard_lease(lease)
        row = (
            await self._session.scalars(
                select(ToolInvocationRow)
                .where(ToolInvocationRow.id == invocation_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("tool invocation not found")
        current = invocation_to_domain(row)
        if current.status is not expected_status:
            raise ConflictError(f"expected {expected_status.value}, found {current.status.value}")
        if invocation.id != invocation_id or invocation.run_id != current.run_id:
            raise ConflictError("tool invocation identity cannot change")
        if invocation.status not in ALLOWED_TOOL_TRANSITIONS[current.status]:
            raise ConflictError(
                f"invalid tool transition {current.status.value}->{invocation.status.value}"
            )
        expected = current.model_copy(
            update={
                "status": invocation.status,
                "effect_sent_at": invocation.effect_sent_at,
                "effective_arguments_hash": invocation.effective_arguments_hash,
                "suspended_kind": invocation.suspended_kind,
                "suspended_ref": invocation.suspended_ref,
                "policy_decision": invocation.policy_decision,
                "structured_result": invocation.structured_result,
                "output_bytes": invocation.output_bytes,
                "truncated": invocation.truncated,
                "artifact_id": invocation.artifact_id,
                "outcome": invocation.outcome,
                "result_item": invocation.result_item,
                "updated_at": invocation.updated_at,
            },
            deep=True,
        )
        if expected != invocation:
            raise ConflictError("tool transition may not change immutable fields")
        statement = (
            update(ToolInvocationRow)
            .where(
                ToolInvocationRow.id == invocation_id,
                ToolInvocationRow.status == expected_status.value,
            )
            .values(
                status=invocation.status.value,
                effect_sent_at=invocation.effect_sent_at,
                effective_arguments_hash=invocation.effective_arguments_hash,
                suspended_kind=invocation.suspended_kind,
                suspended_ref=invocation.suspended_ref,
                policy_decision=(
                    None
                    if invocation.policy_decision is None
                    else invocation.policy_decision.model_dump(mode="json")
                ),
                structured_result=invocation.structured_result,
                output_bytes=invocation.output_bytes,
                truncated=invocation.truncated,
                artifact_id=invocation.artifact_id,
                outcome=(
                    None
                    if invocation.outcome is None
                    else invocation.outcome.model_dump(mode="json")
                ),
                result_item=(
                    None
                    if invocation.result_item is None
                    else invocation.result_item.model_dump(mode="json")
                ),
                outcome_status=(
                    None if invocation.outcome is None else invocation.outcome.status.value
                ),
                reason_code=(
                    None if invocation.outcome is None else invocation.outcome.reason_code
                ),
                updated_at=invocation.updated_at,
            )
            .returning(ToolInvocationRow)
        )
        updated = (await self._session.scalars(statement)).one_or_none()
        if updated is None:
            raise ConflictError("tool invocation transition lost a concurrency race")
        return invocation_to_domain(updated)

    async def list_for_run(self, run_id: UUID, principal: Principal) -> list[ToolInvocation]:
        await self._runs.get(run_id, principal)
        rows = (
            await self._session.scalars(
                select(ToolInvocationRow)
                .where(ToolInvocationRow.run_id == run_id)
                .order_by(
                    ToolInvocationRow.step_number,
                    ToolInvocationRow.created_at,
                    ToolInvocationRow.id,
                )
            )
        ).all()
        return [invocation_to_domain(row) for row in rows]


class PostgresApprovalRepository:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        statement = (
            pg_insert(ApprovalRow)
            .values(**approval_values(request))
            .on_conflict_do_nothing(index_elements=[ApprovalRow.action_id])
            .returning(ApprovalRow)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            raise ConflictError("approval already exists for action")
        return approval_to_domain(row)

    async def discard_pending(self, approval_id: UUID) -> None:
        result = await self._session.execute(
            delete(ApprovalRow).where(
                ApprovalRow.id == approval_id,
                ApprovalRow.status == ApprovalStatus.PENDING.value,
            )
        )
        if not _rowcount(result):
            row = await self._session.get(ApprovalRow, approval_id)
            if row is not None:
                raise ConflictError("only a pending approval can be discarded")

    async def get(self, approval_id: UUID, principal: Principal) -> ApprovalRequest:
        row = (
            await self._session.scalars(
                select(ApprovalRow).where(
                    ApprovalRow.id == approval_id,
                    ApprovalRow.tenant_id == principal.tenant_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("approval not found")
        return approval_to_domain(row)

    async def get_by_action(self, action_id: UUID) -> ApprovalRequest | None:
        row = (
            await self._session.scalars(
                select(ApprovalRow).where(ApprovalRow.action_id == action_id)
            )
        ).one_or_none()
        return None if row is None else approval_to_domain(row)

    async def record_revalidation(self, action_id: UUID, policy_version: str) -> ApprovalRequest:
        row = (
            await self._session.scalars(
                update(ApprovalRow)
                .where(ApprovalRow.action_id == action_id)
                .values(revalidated_policy_version=policy_version)
                .returning(ApprovalRow)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("approval not found")
        return approval_to_domain(row)

    async def list_pending(
        self,
        principal: Principal,
        run_id: UUID | None = None,
        session_id: UUID | None = None,
        limit: int = 50,
        cursor: ApprovalCursor | None = None,
    ) -> list[ApprovalRequest]:
        predicates: list[Any] = [
            ApprovalRow.tenant_id == principal.tenant_id,
            ApprovalRow.status == ApprovalStatus.PENDING.value,
        ]
        if run_id is not None:
            predicates.append(ApprovalRow.run_id == run_id)
        if session_id is not None:
            predicates.append(ApprovalRow.session_id == session_id)
        if cursor is not None:
            predicates.append(
                (ApprovalRow.created_at < cursor.created_at)
                | ((ApprovalRow.created_at == cursor.created_at) & (ApprovalRow.id < cursor.id))
            )
        rows = (
            await self._session.scalars(
                select(ApprovalRow)
                .where(*predicates)
                .order_by(ApprovalRow.created_at.desc(), ApprovalRow.id.desc())
                .limit(limit)
            )
        ).all()
        return [approval_to_domain(row) for row in rows]

    async def resolve(
        self,
        approval_id: UUID,
        principal: Principal,
        resolution: ApprovalResolutionType,
        reason: str | None,
    ) -> ApprovalResolutionOutcome:
        row = (
            await self._session.scalars(
                select(ApprovalRow)
                .where(
                    ApprovalRow.id == approval_id,
                    ApprovalRow.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("approval not found")
        current = approval_to_domain(row)
        if current.status is not ApprovalStatus.PENDING:
            state = (
                ApprovalResolutionState.ALREADY_RESOLVED_IDENTICALLY
                if current.resolution is resolution
                else ApprovalResolutionState.ALREADY_RESOLVED_DIFFERENTLY
            )
            return ApprovalResolutionOutcome(state=state, approval=current)
        status = (
            ApprovalStatus.APPROVED
            if resolution is ApprovalResolutionType.APPROVE_ONCE
            else ApprovalStatus.DENIED
        )
        updated_row = (
            await self._session.scalars(
                update(ApprovalRow)
                .where(
                    ApprovalRow.id == approval_id,
                    ApprovalRow.status == ApprovalStatus.PENDING.value,
                )
                .values(
                    status=status.value,
                    resolution={"resolution": resolution.value, "reason": reason},
                    resolved_at=self._clock.now(),
                    resolved_by=principal.principal_id,
                )
                .returning(ApprovalRow)
            )
        ).one_or_none()
        if updated_row is None:
            raise ConflictError("approval resolution lost a concurrency race")
        return ApprovalResolutionOutcome(
            state=ApprovalResolutionState.APPLIED,
            approval=approval_to_domain(updated_row),
        )

    async def expire_due(
        self, now: datetime, limit: int, *, tenant_id: str
    ) -> list[ApprovalRequest]:
        rows = (
            await self._session.scalars(
                select(ApprovalRow)
                .where(
                    ApprovalRow.tenant_id == tenant_id,
                    ApprovalRow.status == ApprovalStatus.PENDING.value,
                    ApprovalRow.expires_at.is_not(None),
                    ApprovalRow.expires_at <= now,
                )
                .order_by(ApprovalRow.expires_at, ApprovalRow.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        result: list[ApprovalRequest] = []
        for row in rows:
            row.status = ApprovalStatus.EXPIRED.value
            row.resolved_at = now
            result.append(approval_to_domain(row))
        return result

    async def cancel_for_run(self, run_id: UUID) -> int:
        result = await self._session.execute(
            update(ApprovalRow)
            .where(
                ApprovalRow.run_id == run_id,
                ApprovalRow.status == ApprovalStatus.PENDING.value,
            )
            .values(status=ApprovalStatus.CANCELLED.value, resolved_at=self._clock.now())
        )
        return _rowcount(result)


class PostgresPolicyProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, profile: PolicyProfileRecord) -> PolicyProfileRecord:
        values = profile.model_dump()
        await self._session.execute(
            pg_insert(PolicyProfileRow)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[PolicyProfileRow.policy_version])
        )
        stored = await self.get(profile.policy_version)
        if stored is None:
            raise NotFoundError("policy profile audit record was not persisted")
        immutable = {"loaded_at", "loaded_by"}
        if stored.model_dump(exclude=immutable) != profile.model_dump(exclude=immutable):
            raise ConflictError("policy version identifies different rules")
        return stored

    async def get(self, policy_version: str) -> PolicyProfileRecord | None:
        row = await self._session.get(PolicyProfileRow, policy_version)
        if row is None:
            return None
        return PolicyProfileRecord(
            policy_version=row.policy_version,
            profile_name=row.profile_name,
            profile_sha256=row.profile_sha256,
            hardline_sha256=row.hardline_sha256,
            rule_count=row.rule_count,
            loaded_at=row.loaded_at,
            loaded_by=row.loaded_by,
        )


def _browser_profile_to_domain(row: BrowserProfileRow) -> BrowserProfile:
    return BrowserProfile(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        provider_name=row.provider_name,
        provider_ref=row.provider_ref,
        allowed_origins=tuple(row.allowed_origins),
        status=BrowserProfileStatus(row.status),
        generation=row.generation,
        encryption_key_version=row.encryption_key_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_used_at=row.last_used_at,
    )


class PostgresBrowserProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _values(profile: BrowserProfile) -> dict[str, Any]:
        return {
            "id": profile.id,
            "tenant_id": profile.tenant_id,
            "principal_id": profile.principal_id,
            "provider_name": profile.provider_name,
            "provider_ref": profile.provider_ref,
            "allowed_origins": list(profile.allowed_origins),
            "status": profile.status.value,
            "generation": profile.generation,
            "encryption_key_version": profile.encryption_key_version,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
            "last_used_at": profile.last_used_at,
        }

    async def create(self, profile: BrowserProfile) -> BrowserProfile:
        statement = (
            pg_insert(BrowserProfileRow)
            .values(**self._values(profile))
            .on_conflict_do_nothing(index_elements=[BrowserProfileRow.id])
        )
        if not _rowcount(await self._session.execute(statement)):
            raise ConflictError("browser profile already exists")
        return profile.model_copy(deep=True)

    async def get(self, profile_id: UUID, principal: Principal) -> BrowserProfile:
        row = (
            await self._session.scalars(
                select(BrowserProfileRow).where(
                    BrowserProfileRow.id == profile_id,
                    BrowserProfileRow.tenant_id == principal.tenant_id,
                    BrowserProfileRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("browser profile not found")
        return _browser_profile_to_domain(row)

    async def list(
        self,
        principal: Principal,
        *,
        limit: int | None = None,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[BrowserProfile]:
        if (after_created_at is None) != (after_id is None):
            raise ValueError("pagination cursor components must be provided together")
        statement = select(BrowserProfileRow).where(
            BrowserProfileRow.tenant_id == principal.tenant_id,
            BrowserProfileRow.principal_id == principal.principal_id,
        )
        if after_created_at is not None and after_id is not None:
            statement = statement.where(
                or_(
                    BrowserProfileRow.created_at > after_created_at,
                    and_(
                        BrowserProfileRow.created_at == after_created_at,
                        BrowserProfileRow.id > after_id,
                    ),
                )
            )
        statement = statement.order_by(BrowserProfileRow.created_at, BrowserProfileRow.id)
        if limit is not None:
            statement = statement.limit(limit)
        rows = (await self._session.scalars(statement)).all()
        return [_browser_profile_to_domain(row) for row in rows]

    async def bind(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
        provisioning: BrowserProfileProvisioning,
        updated_at: datetime,
    ) -> BrowserProfile:
        statement = (
            update(BrowserProfileRow)
            .where(
                BrowserProfileRow.id == profile_id,
                BrowserProfileRow.tenant_id == principal.tenant_id,
                BrowserProfileRow.principal_id == principal.principal_id,
                BrowserProfileRow.generation == expected_generation,
                BrowserProfileRow.status == BrowserProfileStatus.PROVISIONING.value,
                BrowserProfileRow.updated_at <= updated_at,
            )
            .values(
                provider_name=provisioning.provider_name,
                provider_ref=provisioning.provider_ref,
                encryption_key_version=provisioning.encryption_key_version,
                status=BrowserProfileStatus.AUTHENTICATION_REQUIRED.value,
                generation=BrowserProfileRow.generation + 1,
                updated_at=updated_at,
            )
            .returning(BrowserProfileRow)
        )
        try:
            async with self._session.begin_nested():
                row = (await self._session.scalars(statement)).one_or_none()
        except IntegrityError as exc:
            if _constraint_name(exc) == "uq_browser_profiles_provider_ref":
                raise ConflictError("browser provider reference is already bound") from exc
            raise
        if row is not None:
            return _browser_profile_to_domain(row)
        current = await self.get(profile_id, principal)
        if current.generation != expected_generation:
            raise ConcurrencyConflict("browser profile generation changed")
        if updated_at < current.updated_at:
            raise ConflictError("browser profile update time moved backwards")
        raise ConflictError("browser profile is not awaiting a provider binding")

    async def transition(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
        status: BrowserProfileStatus,
        updated_at: datetime,
    ) -> BrowserProfile:
        current = await self.get(profile_id, principal)
        if current.generation != expected_generation:
            raise ConcurrencyConflict("browser profile generation changed")
        if current.status is status:
            return current
        if status not in ALLOWED_BROWSER_PROFILE_TRANSITIONS[current.status]:
            raise ConflictError("browser profile transition is not allowed")
        if updated_at < current.updated_at:
            raise ConflictError("browser profile update time moved backwards")
        row = (
            await self._session.scalars(
                update(BrowserProfileRow)
                .where(
                    BrowserProfileRow.id == profile_id,
                    BrowserProfileRow.tenant_id == principal.tenant_id,
                    BrowserProfileRow.principal_id == principal.principal_id,
                    BrowserProfileRow.generation == expected_generation,
                    BrowserProfileRow.status == current.status.value,
                )
                .values(
                    status=status.value,
                    generation=BrowserProfileRow.generation + 1,
                    updated_at=updated_at,
                )
                .returning(BrowserProfileRow)
            )
        ).one_or_none()
        if row is None:
            raise ConcurrencyConflict("browser profile generation changed")
        return _browser_profile_to_domain(row)

    async def delete(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
    ) -> None:
        deleted = _rowcount(
            await self._session.execute(
                delete(BrowserProfileRow).where(
                    BrowserProfileRow.id == profile_id,
                    BrowserProfileRow.tenant_id == principal.tenant_id,
                    BrowserProfileRow.principal_id == principal.principal_id,
                    BrowserProfileRow.generation == expected_generation,
                    BrowserProfileRow.status == BrowserProfileStatus.REVOKED.value,
                )
            )
        )
        if deleted:
            return
        try:
            current = await self.get(profile_id, principal)
        except NotFoundError:
            return
        if current.generation != expected_generation:
            raise ConcurrencyConflict("browser profile generation changed")
        raise ConflictError("browser profile must be revoked before deletion")


def _browser_grant_to_domain(row: BrowserGrantRow) -> BrowserGrant:
    return BrowserGrant(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        profile_id=row.profile_id,
        profile_generation=row.profile_generation,
        agent_version=row.agent_version,
        policy_version=row.policy_version,
        allowed_origins=tuple(row.allowed_origins),
        action_kinds=tuple(BrowserActionKind(value) for value in row.action_kinds),
        element_roles=tuple(row.element_roles),
        element_names=tuple(row.element_names),
        purpose=row.purpose,
        starts_at=row.starts_at,
        expires_at=row.expires_at,
        approved_by=row.approved_by,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresBrowserGrantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _values(grant: BrowserGrant) -> dict[str, Any]:
        return {
            "id": grant.id,
            "tenant_id": grant.tenant_id,
            "principal_id": grant.principal_id,
            "profile_id": grant.profile_id,
            "profile_generation": grant.profile_generation,
            "agent_version": grant.agent_version,
            "policy_version": grant.policy_version,
            "allowed_origins": list(grant.allowed_origins),
            "action_kinds": [kind.value for kind in grant.action_kinds],
            "element_roles": list(grant.element_roles),
            "element_names": list(grant.element_names),
            "purpose": grant.purpose,
            "starts_at": grant.starts_at,
            "expires_at": grant.expires_at,
            "approved_by": grant.approved_by,
            "revoked_at": grant.revoked_at,
            "created_at": grant.created_at,
            "updated_at": grant.updated_at,
        }

    async def create(self, grant: BrowserGrant) -> BrowserGrant:
        statement = (
            pg_insert(BrowserGrantRow)
            .values(**self._values(grant))
            .on_conflict_do_nothing(index_elements=[BrowserGrantRow.id])
        )
        if not _rowcount(await self._session.execute(statement)):
            raise ConflictError("browser grant already exists")
        return grant.model_copy(deep=True)

    async def get(self, grant_id: UUID, principal: Principal) -> BrowserGrant:
        row = (
            await self._session.scalars(
                select(BrowserGrantRow).where(
                    BrowserGrantRow.id == grant_id,
                    BrowserGrantRow.tenant_id == principal.tenant_id,
                    BrowserGrantRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("browser grant not found")
        return _browser_grant_to_domain(row)

    async def list(
        self,
        principal: Principal,
        *,
        profile_id: UUID | None = None,
        limit: int | None = None,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[BrowserGrant]:
        if (after_created_at is None) != (after_id is None):
            raise ValueError("pagination cursor components must be provided together")
        statement = select(BrowserGrantRow).where(
            BrowserGrantRow.tenant_id == principal.tenant_id,
            BrowserGrantRow.principal_id == principal.principal_id,
        )
        if profile_id is not None:
            statement = statement.where(BrowserGrantRow.profile_id == profile_id)
        if after_created_at is not None and after_id is not None:
            statement = statement.where(
                or_(
                    BrowserGrantRow.created_at > after_created_at,
                    and_(
                        BrowserGrantRow.created_at == after_created_at,
                        BrowserGrantRow.id > after_id,
                    ),
                )
            )
        statement = statement.order_by(BrowserGrantRow.created_at, BrowserGrantRow.id)
        if limit is not None:
            statement = statement.limit(limit)
        rows = (await self._session.scalars(statement)).all()
        return [_browser_grant_to_domain(row) for row in rows]

    async def revoke(
        self,
        grant_id: UUID,
        principal: Principal,
        *,
        revoked_at: datetime,
    ) -> BrowserGrant:
        row = (
            await self._session.scalars(
                update(BrowserGrantRow)
                .where(
                    BrowserGrantRow.id == grant_id,
                    BrowserGrantRow.tenant_id == principal.tenant_id,
                    BrowserGrantRow.principal_id == principal.principal_id,
                    BrowserGrantRow.revoked_at.is_(None),
                    BrowserGrantRow.created_at <= revoked_at,
                    BrowserGrantRow.updated_at <= revoked_at,
                )
                .values(revoked_at=revoked_at, updated_at=revoked_at)
                .returning(BrowserGrantRow)
            )
        ).one_or_none()
        if row is not None:
            return _browser_grant_to_domain(row)
        current = await self.get(grant_id, principal)
        if current.revoked_at is not None:
            return current
        raise ConflictError("browser grant revocation time is invalid")

    async def delete(self, grant_id: UUID, principal: Principal) -> None:
        await self._session.execute(
            delete(BrowserGrantRow).where(
                BrowserGrantRow.id == grant_id,
                BrowserGrantRow.tenant_id == principal.tenant_id,
                BrowserGrantRow.principal_id == principal.principal_id,
            )
        )


def _browser_authentication_to_domain(
    row: BrowserAuthenticationRow,
) -> BrowserAuthenticationRecord:
    return BrowserAuthenticationRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        profile_id=row.profile_id,
        status=BrowserAuthenticationStatus(row.status),
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresBrowserAuthenticationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, authentication: BrowserAuthenticationRecord
    ) -> BrowserAuthenticationRecord:
        values = {
            "id": authentication.id,
            "tenant_id": authentication.tenant_id,
            "principal_id": authentication.principal_id,
            "profile_id": authentication.profile_id,
            "status": authentication.status.value,
            "expires_at": authentication.expires_at,
            "created_at": authentication.created_at,
            "updated_at": authentication.updated_at,
        }
        inserted = _rowcount(
            await self._session.execute(
                pg_insert(BrowserAuthenticationRow)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[BrowserAuthenticationRow.id])
            )
        )
        if not inserted:
            raise ConflictError("browser authentication already exists")
        return authentication.model_copy(deep=True)

    async def get(
        self, authentication_id: UUID, principal: Principal
    ) -> BrowserAuthenticationRecord:
        row = (
            await self._session.scalars(
                select(BrowserAuthenticationRow).where(
                    BrowserAuthenticationRow.id == authentication_id,
                    BrowserAuthenticationRow.tenant_id == principal.tenant_id,
                    BrowserAuthenticationRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("browser authentication not found")
        return _browser_authentication_to_domain(row)

    async def list(
        self,
        principal: Principal,
        *,
        profile_id: UUID | None = None,
    ) -> list[BrowserAuthenticationRecord]:
        statement = select(BrowserAuthenticationRow).where(
            BrowserAuthenticationRow.tenant_id == principal.tenant_id,
            BrowserAuthenticationRow.principal_id == principal.principal_id,
        )
        if profile_id is not None:
            statement = statement.where(BrowserAuthenticationRow.profile_id == profile_id)
        rows = (
            await self._session.scalars(
                statement.order_by(
                    BrowserAuthenticationRow.created_at,
                    BrowserAuthenticationRow.id,
                )
            )
        ).all()
        return [_browser_authentication_to_domain(row) for row in rows]

    async def transition(
        self,
        authentication_id: UUID,
        principal: Principal,
        *,
        expected_status: BrowserAuthenticationStatus,
        status: BrowserAuthenticationStatus,
        updated_at: datetime,
    ) -> BrowserAuthenticationRecord:
        allowed = ALLOWED_BROWSER_AUTHENTICATION_TRANSITIONS[expected_status]
        if status is not expected_status and status not in allowed:
            raise ConflictError("browser authentication transition is not allowed")
        row = (
            await self._session.scalars(
                update(BrowserAuthenticationRow)
                .where(
                    BrowserAuthenticationRow.id == authentication_id,
                    BrowserAuthenticationRow.tenant_id == principal.tenant_id,
                    BrowserAuthenticationRow.principal_id == principal.principal_id,
                    BrowserAuthenticationRow.status == expected_status.value,
                    BrowserAuthenticationRow.updated_at <= updated_at,
                )
                .values(status=status.value, updated_at=updated_at)
                .returning(BrowserAuthenticationRow)
            )
        ).one_or_none()
        if row is not None:
            return _browser_authentication_to_domain(row)
        current = await self.get(authentication_id, principal)
        if current.status is expected_status and updated_at < current.updated_at:
            raise ConflictError("browser authentication update time moved backwards")
        raise ConflictError("browser authentication status changed")


class PostgresProcessEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: ProcessEventRow) -> ProcessEvent:
        return ProcessEvent(
            id=row.id,
            event_type=row.event_type,
            payload_schema_version=row.payload_schema_version,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            payload=dict(row.payload),
            derivation_key=row.derivation_key,
            created_at=row.created_at,
        )

    async def append(self, event: ProcessEvent) -> ProcessEvent:
        await self._session.execute(
            pg_insert(ProcessEventRow)
            .values(**event.model_dump())
            .on_conflict_do_nothing(index_elements=[ProcessEventRow.derivation_key])
        )
        row = (
            await self._session.scalars(
                select(ProcessEventRow).where(
                    ProcessEventRow.derivation_key == event.derivation_key
                )
            )
        ).one()
        stored = self._to_domain(row)
        if stored.model_dump(exclude={"created_at"}) != event.model_dump(exclude={"created_at"}):
            raise ConflictError("process event derivation identifies different content")
        return stored

    async def get_by_derivation(self, derivation_key: str) -> ProcessEvent | None:
        row = (
            await self._session.scalars(
                select(ProcessEventRow).where(ProcessEventRow.derivation_key == derivation_key)
            )
        ).one_or_none()
        return None if row is None else self._to_domain(row)

    async def list(self, event_type: str | None = None) -> list[ProcessEvent]:
        statement = select(ProcessEventRow)
        if event_type is not None:
            statement = statement.where(ProcessEventRow.event_type == event_type)
        rows = (
            await self._session.scalars(
                statement.order_by(ProcessEventRow.created_at, ProcessEventRow.id)
            )
        ).all()
        return [self._to_domain(row) for row in rows]


class PostgresIdempotencyRepository:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def get(self, key: str, tenant_id: str, principal_id: str) -> IdempotencyRecord | None:
        lock_key = f"request:{tenant_id}:{principal_id}:{key}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
        row = (
            await self._session.scalars(
                select(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.key == key,
                    IdempotencyKeyRow.tenant_id == tenant_id,
                    IdempotencyKeyRow.principal_id == principal_id,
                    IdempotencyKeyRow.expires_at > self._clock.now(),
                )
            )
        ).one_or_none()
        return None if row is None else idempotency_to_domain(row)

    async def create(self, record: IdempotencyRecord) -> IdempotencyRecord:
        lock_key = f"request:{record.tenant_id}:{record.principal_id}:{record.key}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
        await self._session.execute(
            delete(IdempotencyKeyRow).where(
                IdempotencyKeyRow.key == record.key,
                IdempotencyKeyRow.tenant_id == record.tenant_id,
                IdempotencyKeyRow.principal_id == record.principal_id,
                IdempotencyKeyRow.expires_at <= self._clock.now(),
            )
        )
        statement = (
            pg_insert(IdempotencyKeyRow)
            .values(**idempotency_values(record))
            .on_conflict_do_nothing(
                index_elements=[
                    IdempotencyKeyRow.tenant_id,
                    IdempotencyKeyRow.principal_id,
                    IdempotencyKeyRow.key,
                ]
            )
            .returning(IdempotencyKeyRow)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is not None:
            return idempotency_to_domain(row)
        existing = await self.get(record.key, record.tenant_id, record.principal_id)
        if existing is None:
            raise ConcurrencyConflict("idempotency reservation disappeared")
        if existing.request_hash != record.request_hash:
            raise ConflictError("idempotency key was reused with a different request")
        return existing


class PostgresUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_attempt(self, call: ModelCallRecord) -> None:
        await self._session.execute(
            pg_insert(ModelCallRow)
            .values(**model_call_values(call))
            .on_conflict_do_nothing(index_elements=[ModelCallRow.attempt_id])
        )

    async def run_usage(self, run_id: UUID) -> RunUsage:
        row = await self._session.get(RunRow, run_id)
        if row is None:
            raise NotFoundError("run not found")
        return RunUsage.model_validate(row.usage)

    async def tenant_usage(
        self, tenant_id: str, *, since: datetime, until: datetime
    ) -> UsageRollup:
        statement = select(
            func.coalesce(func.sum(ModelCallRow.input_tokens), 0),
            func.coalesce(func.sum(ModelCallRow.cached_input_tokens), 0),
            func.coalesce(func.sum(ModelCallRow.cache_write_tokens), 0),
            func.coalesce(func.sum(ModelCallRow.output_tokens), 0),
            func.sum(ModelCallRow.reasoning_tokens),
            func.coalesce(func.sum(ModelCallRow.cost), Decimal("0")),
        ).where(
            ModelCallRow.tenant_id == tenant_id,
            ModelCallRow.started_at >= since,
            ModelCallRow.started_at < until,
        )
        values = (await self._session.execute(statement)).one()
        return UsageRollup(
            input_tokens=values[0],
            cached_input_tokens=values[1],
            cache_write_input_tokens=values[2],
            output_tokens=values[3],
            reasoning_tokens=values[4],
            cost=values[5],
        )


class PostgresExportConsentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tenant_id: str, principal_id: str) -> ExportConsent | None:
        row = await self._session.get(ExportConsentRow, (tenant_id, principal_id))
        if row is None:
            return None
        return ExportConsent(
            tenant_id=row.tenant_id,
            principal_id=row.principal_id,
            granted_at=row.granted_at,
            withdrawn_at=row.withdrawn_at,
        )

    async def get_for_update(self, tenant_id: str, principal_id: str) -> ExportConsent | None:
        row = (
            await self._session.scalars(
                select(ExportConsentRow)
                .where(
                    ExportConsentRow.tenant_id == tenant_id,
                    ExportConsentRow.principal_id == principal_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            return None
        return ExportConsent(
            tenant_id=row.tenant_id,
            principal_id=row.principal_id,
            granted_at=row.granted_at,
            withdrawn_at=row.withdrawn_at,
        )

    async def grant(self, consent: ExportConsent) -> ExportConsent:
        statement = (
            pg_insert(ExportConsentRow)
            .values(**consent.model_dump())
            .on_conflict_do_update(
                index_elements=[ExportConsentRow.tenant_id, ExportConsentRow.principal_id],
                set_={"granted_at": consent.granted_at, "withdrawn_at": None},
            )
            .returning(ExportConsentRow)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            raise NotFoundError("export consent not found after grant")
        return ExportConsent(
            tenant_id=row.tenant_id,
            principal_id=row.principal_id,
            granted_at=row.granted_at,
            withdrawn_at=row.withdrawn_at,
        )

    async def withdraw(
        self, tenant_id: str, principal_id: str, withdrawn_at: datetime
    ) -> ExportConsent:
        statement = (
            update(ExportConsentRow)
            .where(
                ExportConsentRow.tenant_id == tenant_id,
                ExportConsentRow.principal_id == principal_id,
                ExportConsentRow.withdrawn_at.is_(None),
            )
            .values(withdrawn_at=withdrawn_at)
            .returning(ExportConsentRow)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            row = (
                await self._session.scalars(
                    select(ExportConsentRow).where(
                        ExportConsentRow.tenant_id == tenant_id,
                        ExportConsentRow.principal_id == principal_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError("export consent not found")
        return ExportConsent(
            tenant_id=row.tenant_id,
            principal_id=row.principal_id,
            granted_at=row.granted_at,
            withdrawn_at=row.withdrawn_at,
        )


class PostgresTrajectoryExportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_run(self, run_id: UUID) -> TrajectoryExport | None:
        row = (
            await self._session.execute(
                select(TrajectoryExportRow, ArtifactRow)
                .join(ArtifactRow, ArtifactRow.id == TrajectoryExportRow.artifact_id)
                .where(TrajectoryExportRow.run_id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return trajectory_export_to_domain(row[0], row[1])

    async def create(self, export: TrajectoryExport) -> TrajectoryExport:
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"export:{export.run_id}", 0)))
        )
        existing = await self.get_for_run(export.run_id)
        if existing is not None:
            return existing
        await self._session.execute(
            pg_insert(ArtifactRow).values(**artifact_values(export.artifact))
        )
        await self._session.execute(
            pg_insert(TrajectoryExportRow).values(**trajectory_export_values(export))
        )
        return export.model_copy(deep=True)

    async def get_artifact(self, artifact_id: UUID, principal: Principal) -> ArtifactRef:
        row = (
            await self._session.scalars(
                select(ArtifactRow)
                .join(
                    TrajectoryExportRow,
                    TrajectoryExportRow.artifact_id == ArtifactRow.id,
                )
                .where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.tenant_id == principal.tenant_id,
                    ArtifactRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("artifact not found")
        return artifact_to_domain(row)

    async def expire_for_principal(
        self, tenant_id: str, principal_id: str, expired_at: datetime
    ) -> int:
        artifact_ids = select(TrajectoryExportRow.artifact_id).where(
            TrajectoryExportRow.tenant_id == tenant_id,
            TrajectoryExportRow.principal_id == principal_id,
        )
        result = await self._session.execute(
            update(ArtifactRow)
            .where(
                ArtifactRow.id.in_(artifact_ids),
                ArtifactRow.expires_at > expired_at,
            )
            .values(expires_at=expired_at)
        )
        return _rowcount(result)

    async def list_expired(self, now: datetime, *, limit: int) -> list[ArtifactRef]:
        rows = list(
            (
                await self._session.scalars(
                    select(ArtifactRow)
                    .where(
                        ArtifactRow.origin == "trajectory_export",
                        ArtifactRow.expires_at <= now,
                    )
                    .order_by(ArtifactRow.expires_at, ArtifactRow.id)
                    .limit(limit)
                )
            ).all()
        )
        return [artifact_to_domain(row) for row in rows]

    async def delete_expired(self, artifact_id: UUID, *, now: datetime) -> bool:
        result = await self._session.execute(
            delete(ArtifactRow).where(
                ArtifactRow.id == artifact_id,
                ArtifactRow.origin == "trajectory_export",
                ArtifactRow.expires_at <= now,
            )
        )
        return bool(_rowcount(result))


class PostgresArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, artifact: ArtifactRef) -> ArtifactRef:
        statement = pg_insert(ArtifactRow).values(**artifact_values(artifact))
        statement = statement.on_conflict_do_nothing(index_elements=[ArtifactRow.id])
        await self._session.execute(statement)
        row = await self._session.get(ArtifactRow, artifact.id)
        if row is None:
            raise ConflictError("artifact metadata was not persisted")
        stored = artifact_to_domain(row)
        if stored != artifact:
            raise ConflictError("artifact id already exists with different metadata")
        return stored

    async def exists(self, artifact_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(ArtifactRow.id).where(ArtifactRow.id == artifact_id).limit(1)
            )
        )

    async def get(self, artifact_id: UUID, principal: Principal) -> ArtifactRef:
        row = (
            await self._session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.tenant_id == principal.tenant_id,
                    ArtifactRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("artifact not found")
        return artifact_to_domain(row)

    async def retain_for_knowledge(self, artifact_id: UUID, principal: Principal) -> ArtifactRef:
        row = (
            await self._session.scalars(
                update(ArtifactRow)
                .where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.tenant_id == principal.tenant_id,
                    ArtifactRow.principal_id == principal.principal_id,
                )
                .values(origin="knowledge_source", expires_at=None)
                .returning(ArtifactRow)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("artifact not found")
        return artifact_to_domain(row)

    async def expire(
        self, artifact_id: UUID, principal: Principal, expired_at: datetime
    ) -> ArtifactRef:
        row = (
            await self._session.scalars(
                update(ArtifactRow)
                .where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.tenant_id == principal.tenant_id,
                    ArtifactRow.principal_id == principal.principal_id,
                )
                .values(expires_at=expired_at)
                .returning(ArtifactRow)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("artifact not found")
        return artifact_to_domain(row)

    async def list_expired(self, now: datetime, *, limit: int) -> list[ArtifactRef]:
        rows = list(
            (
                await self._session.scalars(
                    select(ArtifactRow)
                    .where(
                        ArtifactRow.origin != "trajectory_export",
                        ArtifactRow.expires_at.is_not(None),
                        ArtifactRow.expires_at <= now,
                    )
                    .order_by(ArtifactRow.expires_at, ArtifactRow.id)
                    .limit(limit)
                )
            ).all()
        )
        return [artifact_to_domain(row) for row in rows]

    async def delete_expired(self, artifact_id: UUID, *, now: datetime) -> bool:
        result = await self._session.execute(
            delete(ArtifactRow).where(
                ArtifactRow.id == artifact_id,
                ArtifactRow.origin != "trajectory_export",
                ArtifactRow.expires_at <= now,
            )
        )
        return bool(_rowcount(result))


class PostgresMaintenanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _acquire(self, sweep_name: str) -> bool:
        return bool(
            await self._session.scalar(
                select(func.pg_try_advisory_xact_lock(func.hashtextextended(sweep_name, 0)))
            )
        )

    async def live_run_leases(self) -> frozenset[tuple[UUID, int]]:
        rows = (
            await self._session.execute(
                select(RunRow.id, RunRow.lease_epoch).where(
                    RunRow.lease_owner.is_not(None),
                    RunRow.lease_expires_at.is_not(None),
                    RunRow.lease_expires_at > func.now(),
                )
            )
        ).all()
        return frozenset((run_id, int(lease_epoch)) for run_id, lease_epoch in rows)

    async def is_live_run_lease(self, run_id: UUID, lease_epoch: int) -> bool:
        return bool(
            await self._session.scalar(
                select(RunRow.id)
                .where(
                    RunRow.id == run_id,
                    RunRow.lease_epoch == lease_epoch,
                    RunRow.lease_owner.is_not(None),
                    RunRow.lease_expires_at.is_not(None),
                    RunRow.lease_expires_at > func.now(),
                )
                .limit(1)
            )
        )

    async def projection_sessions(self, limit: int) -> list[UUID]:
        if not await self._acquire("maintenance.session_history"):
            return []
        return list(
            (
                await self._session.scalars(
                    select(SessionRow.id)
                    .outerjoin(
                        ProjectionWatermarkRow,
                        (ProjectionWatermarkRow.projection_name == SESSION_HISTORY_PROJECTION)
                        & (ProjectionWatermarkRow.scope == sql_cast(SessionRow.id, Text)),
                    )
                    .where(
                        SessionRow.next_event_sequence - 1
                        > func.coalesce(ProjectionWatermarkRow.watermark_seq, 0)
                    )
                    .order_by(
                        ProjectionWatermarkRow.updated_at.asc().nulls_first(),
                        SessionRow.updated_at,
                    )
                    .limit(limit)
                )
            ).all()
        )

    async def checkpoint_runs(self, limit: int) -> list[tuple[UUID, bool]]:
        if not await self._acquire("maintenance.checkpoint_prune"):
            return []
        rows = (
            await self._session.execute(
                select(
                    CheckpointRow.run_id,
                    RunRow.status,
                    func.min(CheckpointRow.created_at).label("oldest_checkpoint"),
                )
                .join(RunRow, RunRow.id == CheckpointRow.run_id)
                .group_by(CheckpointRow.run_id, RunRow.status)
                .having(func.count(CheckpointRow.id) > 1)
                .order_by(func.min(CheckpointRow.created_at), CheckpointRow.run_id)
                .limit(limit)
            )
        ).all()
        return [
            (
                run_id,
                RunStatus(status) in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED},
            )
            for run_id, status, _oldest_checkpoint in rows
        ]

    async def trajectory_runs(self, limit: int) -> list[UUID]:
        if not await self._acquire("maintenance.trajectory_projection"):
            return []
        watermark = func.coalesce(ProjectionWatermarkRow.watermark_seq, 0)
        rows = (
            await self._session.scalars(
                select(EventRow.run_id)
                .outerjoin(
                    ProjectionWatermarkRow,
                    (ProjectionWatermarkRow.projection_name == TRAJECTORY_PROJECTION)
                    & (ProjectionWatermarkRow.scope == sql_cast(EventRow.run_id, Text)),
                )
                .where(EventRow.run_id.is_not(None))
                .group_by(
                    EventRow.run_id,
                    ProjectionWatermarkRow.watermark_seq,
                    ProjectionWatermarkRow.updated_at,
                )
                .having(func.max(EventRow.sequence) > watermark)
                .order_by(
                    ProjectionWatermarkRow.updated_at.asc().nulls_first(),
                    func.min(EventRow.created_at),
                )
                .limit(limit)
            )
        ).all()
        return [run_id for run_id in rows if run_id is not None]

    async def pending_memory_sessions(
        self,
        principal: Principal,
        *,
        idle_before: datetime,
        ready_at: datetime,
        limit: int,
    ) -> list[UUID]:
        if limit <= 0:
            return []
        if not await self._acquire("maintenance.memory_formation"):
            return []
        watermark = func.coalesce(ConsolidationWatermarkRow.sequence, 0)
        raw_not_before = EventRow.payload["not_before"].astext
        formation_not_before = case(
            (raw_not_before.is_(None), EventRow.created_at),
            (
                raw_not_before.op("~")(r"(Z|[+-][0-9]{2}:[0-9]{2})$")
                & func.pg_input_is_valid(raw_not_before, "timestamp with time zone"),
                sql_cast(raw_not_before, DateTime(timezone=True)),
            ),
            else_=None,
        )
        rows = (
            await self._session.scalars(
                select(SessionRow.id)
                .join(EventRow, EventRow.session_id == SessionRow.id)
                .outerjoin(
                    ConsolidationWatermarkRow,
                    (ConsolidationWatermarkRow.session_id == SessionRow.id)
                    & (ConsolidationWatermarkRow.tenant_id == principal.tenant_id)
                    & (ConsolidationWatermarkRow.principal_id == principal.principal_id),
                )
                .where(
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                    SessionRow.updated_at <= idle_before,
                    EventRow.event_type == "memory.formation.requested",
                    formation_not_before <= ready_at,
                )
                .group_by(SessionRow.id, ConsolidationWatermarkRow.sequence)
                .having(func.max(EventRow.sequence) > watermark)
                .order_by(SessionRow.updated_at, SessionRow.id)
                .limit(limit)
            )
        ).all()
        return list(rows)

    async def acquire_memory_session(self, principal: Principal, session_id: UUID) -> bool:
        return await self._acquire(
            "maintenance.memory_formation:"
            f"{principal.tenant_id}:{principal.principal_id}:{session_id}"
        )

    async def release_memory_session(self, principal: Principal, session_id: UUID) -> None:
        # pg_try_advisory_xact_lock is released by the surrounding transaction.
        del principal, session_id


class PostgresCapabilityEvaluationRepository:
    """PostgreSQL capability results with one row per build repeat."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _run(row: EvalScenarioRunRow) -> EvalScenarioRun:
        return EvalScenarioRun(
            id=row.id,
            scenario_id=row.scenario_id,
            suite=row.suite,
            repeat_index=row.repeat_index,
            run_id=row.run_id,
            judge_version=row.judge_version,
            build_ref=row.build_ref,
            score=row.score,
            ceiling_hit=row.ceiling_hit,
            policy_failures=row.policy_failures,
            cost_usd=row.cost_usd,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    @staticmethod
    def _criterion(row: EvalCriterionScoreRow) -> EvalCriterionScore:
        return EvalCriterionScore(
            id=row.id,
            scenario_run_id=row.scenario_run_id,
            criterion=row.criterion,
            observation=row.observation,
            value=row.value,
        )

    async def _saved(
        self,
        row: EvalScenarioRunRow,
        *,
        scores: Sequence[EvalCriterionScoreRow] | None = None,
        replaced: bool = False,
    ) -> SavedEvalScenario:
        if scores is None:
            scores = list(
                (
                    await self._session.scalars(
                        select(EvalCriterionScoreRow)
                        .where(EvalCriterionScoreRow.scenario_run_id == row.id)
                        .order_by(EvalCriterionScoreRow.criterion)
                    )
                ).all()
            )
        return SavedEvalScenario(
            run=self._run(row),
            criteria=[self._criterion(score) for score in scores],
            replaced=replaced,
        )

    async def replace(
        self,
        run: EvalScenarioRun,
        criteria: Sequence[EvalCriterionScore],
    ) -> SavedEvalScenario:
        if len({score.criterion for score in criteria}) != len(criteria):
            raise ConflictError("criterion names must be unique within a scenario run")
        values = run.model_dump()
        key_filter = (
            EvalScenarioRunRow.scenario_id == run.scenario_id,
            EvalScenarioRunRow.build_ref == run.build_ref,
            EvalScenarioRunRow.judge_version == run.judge_version,
            EvalScenarioRunRow.repeat_index == run.repeat_index,
        )
        update_values = {key: value for key, value in values.items() if key != "id"}
        row = (
            await self._session.scalars(
                pg_insert(EvalScenarioRunRow)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_eval_scenario_run_build_repeat")
                .returning(EvalScenarioRunRow),
                execution_options={"populate_existing": True},
            )
        ).one_or_none()
        replaced = row is None
        if row is None:
            row = (
                await self._session.scalars(
                    update(EvalScenarioRunRow)
                    .where(*key_filter)
                    .values(**update_values)
                    .returning(EvalScenarioRunRow),
                    execution_options={"populate_existing": True},
                )
            ).one_or_none()
        if row is None:
            raise ConflictError("capability scenario result was not persisted")
        stored_id = row.id
        await self._session.execute(
            pg_insert(EvalScenarioAttemptCostRow)
            .values(
                id=run.id,
                scenario_run_id=stored_id,
                cost_usd=run.cost_usd,
                started_at=run.started_at,
            )
            .on_conflict_do_nothing(index_elements=[EvalScenarioAttemptCostRow.id])
        )
        await self._session.execute(
            delete(EvalCriterionScoreRow).where(EvalCriterionScoreRow.scenario_run_id == stored_id)
        )
        if criteria:
            await self._session.execute(
                pg_insert(EvalCriterionScoreRow),
                [
                    {
                        **score.model_dump(exclude={"scenario_run_id"}),
                        "scenario_run_id": stored_id,
                    }
                    for score in criteria
                ],
            )
        return await self._saved(row, replaced=replaced)

    async def get_by_key(
        self,
        scenario_id: str,
        build_ref: str,
        judge_version: str,
        repeat_index: int,
    ) -> SavedEvalScenario | None:
        row = (
            await self._session.scalars(
                select(EvalScenarioRunRow).where(
                    EvalScenarioRunRow.scenario_id == scenario_id,
                    EvalScenarioRunRow.build_ref == build_ref,
                    EvalScenarioRunRow.judge_version == judge_version,
                    EvalScenarioRunRow.repeat_index == repeat_index,
                )
            )
        ).one_or_none()
        return None if row is None else await self._saved(row)

    async def list_for_build(
        self,
        suite: str,
        build_ref: str,
        judge_version: str,
    ) -> list[SavedEvalScenario]:
        rows = list(
            (
                await self._session.scalars(
                    select(EvalScenarioRunRow)
                    .where(
                        EvalScenarioRunRow.suite == suite,
                        EvalScenarioRunRow.build_ref == build_ref,
                        EvalScenarioRunRow.judge_version == judge_version,
                    )
                    .order_by(EvalScenarioRunRow.scenario_id, EvalScenarioRunRow.repeat_index)
                )
            ).all()
        )
        if not rows:
            return []
        scores = list(
            (
                await self._session.scalars(
                    select(EvalCriterionScoreRow)
                    .where(EvalCriterionScoreRow.scenario_run_id.in_([row.id for row in rows]))
                    .order_by(
                        EvalCriterionScoreRow.scenario_run_id,
                        EvalCriterionScoreRow.criterion,
                    )
                )
            ).all()
        )
        scores_by_run: dict[UUID, list[EvalCriterionScoreRow]] = {row.id: [] for row in rows}
        for score in scores:
            scores_by_run[score.scenario_run_id].append(score)
        return [await self._saved(row, scores=scores_by_run[row.id]) for row in rows]

    async def cost_since(self, since: datetime) -> Decimal:
        value = await self._session.scalar(
            select(
                func.coalesce(func.sum(EvalScenarioAttemptCostRow.cost_usd), Decimal("0"))
            ).where(EvalScenarioAttemptCostRow.started_at >= since)
        )
        return Decimal(value or 0)
