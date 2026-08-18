"""Principal-explicit application services for CLI and HTTP entry points."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.application.session_service import bootstrap_session
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import (
    ApprovalCursor,
    ApprovalRequest,
    ApprovalResolutionState,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.artifacts import StoredArtifactRef
from agent_core.domain.canonical import canonical_json
from agent_core.domain.context import WorkingState
from agent_core.domain.errors import (
    AuthorizationError,
    ConflictError,
    InvalidStateTransition,
    NotFoundError,
)
from agent_core.domain.events import EventEnvelope, NewEvent, conversation_items
from agent_core.domain.messages import (
    AssistantMessage,
    FileReferencePart,
    ImageReferencePart,
    TextPart,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.persistence import IdempotencyRecord
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunStatus
from agent_core.domain.sessions import Session, SessionCursor, SessionStatus, conversation_title
from agent_core.domain.tools import ToolInvocationStatus, ToolOutcome, ToolOutcomeStatus
from agent_core.domain.trajectory import ArtifactRef
from agent_core.domain.views import (
    ApprovalFilters,
    ApprovalView,
    ArtifactContent,
    ArtifactView,
    CancelResult,
    ContentBlock,
    FileContentBlock,
    ImageContentBlock,
    Page,
    PersistedStreamFrame,
    RunFailureView,
    RunLimitsView,
    RunUsageView,
    RunView,
    SessionMessageView,
    SessionView,
    StreamFrame,
    SubmitResult,
    TextContentBlock,
    TransientStreamFrame,
)
from agent_core.ports.artifacts import ArtifactStore, TrajectoryArtifactStore
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import RunDispatcher
from agent_core.ports.live_events import LiveEventBroadcaster
from agent_core.ports.persistence import (
    CheckpointSeeder,
    RepositoryUnitOfWork,
    UnitOfWorkFactory,
)
from agent_core.ports.skills import SkillCatalog

type CancelParkedRun = Callable[[RepositoryUnitOfWork, Run, str], Awaitable[Run]]
type ResumeWaitingRun = Callable[[RepositoryUnitOfWork, Run], Awaitable[Run]]
logger = logging.getLogger(__name__)
_IDLE_POLL_SECONDS = 5.0


async def _notify_session_closed(
    callback: Callable[[UUID], Awaitable[None]], session_id: UUID
) -> None:
    """Run post-commit consolidation best-effort without swallowing cancellation."""

    (result,) = await asyncio.gather(callback(session_id), return_exceptions=True)
    if not isinstance(result, BaseException):
        return
    if not isinstance(result, Exception):
        raise result
    logger.warning(
        "session_close_callback_failed",
        extra={"session_id": str(session_id), "error_class": type(result).__name__},
    )


def _session_view(session: Session, latest: Run | None) -> SessionView:
    active = latest if latest is not None and latest.status not in TERMINAL_RUN_STATUSES else None
    return SessionView(
        id=session.id,
        status=session.status,
        agent_id=str(session.agent_id),
        agent_version=session.agent_version,
        title=session.title,
        metadata=dict(session.metadata),
        created_at=session.created_at,
        updated_at=session.updated_at,
        active_run_id=None if active is None else active.id,
        last_run_id=None if latest is None else latest.id,
    )


def _run_view(run: Run) -> RunView:
    failure = None
    if run.status is RunStatus.FAILED and run.failure is not None:
        failure = RunFailureView(
            reason=run.failure.reason,
            message=run.failure.message,
            step_number=run.failure.step_number,
            attempt_number=run.failure.attempt_number,
            occurred_at=run.failure.occurred_at,
        )
    return RunView(
        id=run.id,
        session_id=run.session_id,
        parent_run_id=run.parent_run_id,
        status=run.status,
        step_count=run.step_count,
        model_call_count=run.model_call_count,
        tool_call_count=run.tool_call_count,
        usage=RunUsageView(
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
            cost_usd=str(run.usage.cost),
        ),
        limits=RunLimitsView(
            max_steps=run.limits.max_steps,
            deadline_at=run.limits.deadline_at,
            max_cost_usd=(None if run.limits.max_cost is None else str(run.limits.max_cost)),
        ),
        failure=failure,
        cancel_requested_at=run.cancel_requested_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _approval_view(approval: ApprovalRequest) -> ApprovalView:
    return ApprovalView(
        id=approval.id,
        run_id=approval.run_id,
        session_id=approval.session_id,
        status=approval.status.value.upper(),
        tool_name=approval.tool_name,
        action_summary=approval.action_summary,
        arguments=dict(approval.arguments),
        risk=approval.risk.value.upper(),
        policy_reason=approval.policy_reason,
        expires_at=approval.expires_at,
        created_at=approval.created_at,
        resolved_at=approval.resolved_at,
        resolved_by=approval.resolved_by,
        decision=approval.resolution,
    )


def _artifact_view(artifact: ArtifactRef) -> ArtifactView:
    return ArtifactView(
        id=artifact.id,
        session_id=artifact.session_id,
        run_id=artifact.run_id,
        name=artifact.name,
        media_type=artifact.media_type,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        metadata=dict(artifact.metadata),
        created_at=artifact.created_at,
    )


def _domain_content(
    content: list[ContentBlock],
) -> list[TextPart | ImageReferencePart | FileReferencePart]:
    parts: list[TextPart | ImageReferencePart | FileReferencePart] = []
    for block in content:
        if isinstance(block, TextContentBlock):
            parts.append(TextPart(text=block.text))
        elif isinstance(block, ImageContentBlock):
            parts.append(
                ImageReferencePart(
                    artifact_id=block.artifact_id,
                    media_type=block.media_type,
                    detail=block.detail,
                )
            )
        elif isinstance(block, FileContentBlock):
            parts.append(
                FileReferencePart(
                    artifact_id=block.artifact_id,
                    media_type=block.media_type,
                    filename=block.filename,
                )
            )
    if not parts:
        raise ValueError("content must contain at least one block")
    return parts


def _wire_content(content: list[ContentBlock]) -> list[dict[str, object]]:
    return [block.model_dump(mode="json") for block in content]


def _content_view(
    content: list[TextPart | ImageReferencePart | FileReferencePart],
) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    for part in content:
        if isinstance(part, TextPart):
            blocks.append(TextContentBlock(text=part.text))
        elif isinstance(part, ImageReferencePart):
            blocks.append(
                ImageContentBlock(
                    artifact_id=part.artifact_id,
                    media_type=part.media_type,
                    detail=part.detail,
                )
            )
        elif isinstance(part, FileReferencePart):
            blocks.append(
                FileContentBlock(
                    artifact_id=part.artifact_id,
                    media_type=part.media_type,
                    filename=part.filename,
                )
            )
    return blocks


def _encode_session_cursor(row: Session) -> str:
    payload = json.dumps(
        {"k": row.updated_at.isoformat(), "i": str(row.id)}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_session_cursor(value: str | None) -> SessionCursor | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"k", "i"}:
            raise ValueError
        if not isinstance(raw["k"], str) or not isinstance(raw["i"], str):
            raise ValueError
        updated_at = datetime.fromisoformat(raw["k"])
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError
        return SessionCursor(updated_at=updated_at, id=UUID(raw["i"]))
    except (
        binascii.Error,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("session cursor is malformed") from exc


def _encode_message_cursor(sequence: int) -> str:
    payload = json.dumps({"s": sequence}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_message_cursor(value: str | None) -> int:
    if value is None:
        return 0
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"s"}:
            raise ValueError
        sequence = raw["s"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError
        return sequence
    except (
        binascii.Error,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("session message cursor is malformed") from exc


class PublicSessionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        default_agent: AgentSpec,
        catalogs: SkillCatalog | None = None,
        activate_session: Callable[[UUID], Awaitable[None]] | None = None,
        close_session: Callable[[UUID], Awaitable[None]] | None = None,
        on_session_closed: Callable[[UUID], Awaitable[None]] | None = None,
        trajectory_artifacts: TrajectoryArtifactStore | None = None,
        general_artifacts: ArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._default_agent = default_agent
        self._catalogs = catalogs
        self._activate_session = activate_session
        self._close_session = close_session
        self._on_session_closed = on_session_closed
        self._trajectory_artifacts = trajectory_artifacts
        self._general_artifacts = general_artifacts

    async def _resolve_agent(self, uow: RepositoryUnitOfWork, agent_id: str) -> AgentSpec:
        if agent_id in {"general", str(self._default_agent.id)}:
            return await uow.agents.get_version(self._default_agent.id, self._default_agent.version)
        try:
            requested = UUID(agent_id)
        except ValueError as exc:
            raise NotFoundError("agent not found") from exc
        return await uow.agents.latest_version(requested)

    async def create(
        self,
        principal: Principal,
        agent_id: str,
        metadata: dict[str, object],
    ) -> SessionView:
        require_scope(principal, "session.write")
        encoded = canonical_json(metadata).encode("utf-8")
        if len(encoded) > 8 * 1024:
            raise ValueError("session metadata exceeds 8 KiB")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            agent = await self._resolve_agent(uow, agent_id)
            session_id, catalog = await bootstrap_session(
                uow,
                self._ids,
                self._catalogs,
                self._close_session,
                agent,
                principal,
            )
            session = Session(
                id=session_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                agent_id=agent.id,
                agent_version=agent.version,
                status=SessionStatus.ACTIVE,
                metadata=dict(metadata),
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
                    actor_id=principal.principal_id,
                    payload={
                        "agent_id": str(agent.id),
                        "title": None,
                        "skill_pins": (
                            []
                            if catalog is None
                            else [pin.model_dump(mode="json") for pin in catalog.pins]
                        ),
                        "dropped_skills": ([] if catalog is None else list(catalog.dropped_names)),
                    },
                )
            )
        if self._activate_session is not None:
            await self._activate_session(session.id)
        return _session_view(session, None)

    async def get(self, principal: Principal, session_id: UUID) -> SessionView:
        require_scope(principal, "session.read")
        async with self._uow_factory() as uow:
            session = await uow.sessions.get(session_id, principal)
            latest = await uow.runs.latest_for_session(session_id, principal)
        return _session_view(session, latest)

    async def list(
        self,
        principal: Principal,
        limit: int,
        cursor: str | None,
    ) -> Page[SessionView]:
        require_scope(principal, "session.read")
        effective_limit = min(max(limit, 1), 200)
        decoded = _decode_session_cursor(cursor)
        async with self._uow_factory() as uow:
            rows = await uow.sessions.list(
                principal,
                limit=effective_limit + 1,
                cursor=decoded,
            )
            latest_runs = await uow.runs.latest_for_sessions(
                [row.id for row in rows[:effective_limit]], principal
            )
        has_more = len(rows) > effective_limit
        page_rows = rows[:effective_limit]
        return Page[SessionView](
            items=[_session_view(session, latest_runs.get(session.id)) for session in page_rows],
            next_cursor=(_encode_session_cursor(page_rows[-1]) if has_more and page_rows else None),
        )

    async def messages(
        self,
        principal: Principal,
        session_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> Page[SessionMessageView]:
        require_scope(principal, "session.read")
        effective_limit = min(max(limit, 1), 200)
        after_sequence = _decode_message_cursor(cursor)
        messages: list[SessionMessageView] = []
        async with self._uow_factory() as uow:
            events = await uow.events.list_conversation_after(
                session_id,
                after_sequence,
                principal,
                limit=effective_limit + 1,
            )
            for event in events:
                for item in conversation_items(event):
                    role: Literal["user", "assistant"]
                    if isinstance(item, UserMessage):
                        role = "user"
                    elif isinstance(item, AssistantMessage):
                        role = "assistant"
                    else:
                        continue
                    messages.append(
                        SessionMessageView(
                            sequence=event.sequence,
                            role=role,
                            content=_content_view(item.content),
                        )
                    )
        has_more = len(messages) > effective_limit
        page_messages = messages[:effective_limit]
        return Page[SessionMessageView](
            items=page_messages,
            next_cursor=(
                _encode_message_cursor(page_messages[-1].sequence)
                if has_more and page_messages
                else None
            ),
        )

    async def delete(self, principal: Principal, session_id: UUID) -> None:
        require_scope(principal, "session.write")
        async with self._uow_factory() as uow:
            await uow.session_deletions.delete(session_id, principal, self._clock.now())
        if self._close_session is not None:
            try:
                await self._close_session(session_id)
            except Exception:
                logger.exception("deleted_session_runtime_cleanup_failed")
        if self._catalogs is not None:
            try:
                await self._catalogs.discard(session_id)
            except Exception:
                logger.exception("deleted_session_catalog_cleanup_failed")
        try:
            await self.purge_pending_artifacts(principal, session_id=session_id)
        except Exception:
            logger.exception("deleted_session_artifact_cleanup_failed")

    async def purge_pending_artifacts(
        self,
        principal: Principal,
        *,
        session_id: UUID | None = None,
        limit: int = 100,
    ) -> int:
        if self._trajectory_artifacts is None or self._general_artifacts is None:
            return 0
        if session_id is None:
            async with self._uow_factory() as uow:
                session_ids = await uow.session_deletions.pending_sessions(principal, limit=limit)
        else:
            session_ids = [session_id]
        removed = 0
        for pending_session_id in session_ids:
            async with self._uow_factory() as uow:
                artifacts = await uow.session_deletions.pending_artifacts(
                    pending_session_id,
                    principal,
                    limit=limit,
                )
            for artifact in artifacts:
                try:
                    if artifact.origin == "trajectory_export":
                        await self._trajectory_artifacts.delete(artifact)
                    else:
                        await self._general_artifacts.delete(
                            StoredArtifactRef(
                                artifact_id=artifact.id,
                                sha256=artifact.sha256,
                                size_bytes=artifact.size_bytes,
                                media_type=artifact.media_type,
                            ),
                            tenant_id=artifact.tenant_id,
                        )
                except Exception as exc:
                    logger.warning(
                        "session_artifact_delete_failed",
                        extra={
                            "session_id": str(pending_session_id),
                            "artifact_id": str(artifact.id),
                            "error_class": type(exc).__name__,
                        },
                    )
                    continue
                async with self._uow_factory() as uow:
                    await uow.session_deletions.acknowledge_artifact(
                        pending_session_id,
                        artifact.id,
                        principal,
                    )
                removed += 1
        return removed

    async def close(self, principal: Principal, session_id: UUID) -> SessionView:
        require_scope(principal, "session.write")
        closed_now = False
        async with self._uow_factory() as uow:
            session = await uow.sessions.get(session_id, principal)
            active = await uow.runs.active_for_session(session_id, principal)
            if active is not None:
                raise ConflictError(
                    "The session already has an active run.",
                    reason="active_run_exists",
                    details={"run_id": str(active.id), "run_status": active.status.value},
                )
            session, closed_now = await uow.sessions.close(session_id, principal, self._clock.now())
        if self._close_session is not None:
            await self._close_session(session_id)
        if closed_now and self._on_session_closed is not None:
            await _notify_session_closed(self._on_session_closed, session_id)
        return _session_view(session, None)

    async def ready(self) -> bool:
        """Perform the readiness database round-trip without calling a provider."""

        try:
            async with self._uow_factory() as uow:
                await uow.agents.get_version(self._default_agent.id, self._default_agent.version)
        except Exception as exc:
            logger.warning("readiness_probe_failed", extra={"error_class": type(exc).__name__})
            return False
        return True


class _ExistingRunError(Exception):
    def __init__(self, run: Run) -> None:
        self.run = run


class PublicRunService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        dispatcher: RunDispatcher,
        clock: Clock,
        ids: IdFactory,
        seed_checkpoint: CheckpointSeeder,
        cancel_active: Callable[[UUID | None], None],
        cancel_parked_run: CancelParkedRun,
        resume_waiting_run: ResumeWaitingRun,
        resolve_open_question: Callable[[WorkingState, str | None], WorkingState],
        trajectory_export_enabled: bool,
        live_events: LiveEventBroadcaster,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher
        self._clock = clock
        self._ids = ids
        self._seed_checkpoint = seed_checkpoint
        self._cancel_active = cancel_active
        self._cancel_parked_run = cancel_parked_run
        self._resume_waiting_run = resume_waiting_run
        self._resolve_open_question = resolve_open_question
        self._trajectory_export_enabled = trajectory_export_enabled
        self._live_events = live_events

    async def submit(
        self,
        principal: Principal,
        session_id: UUID,
        content: list[ContentBlock],
        idempotency_key: str | None,
        trace_id: str | None,
    ) -> SubmitResult:
        require_scope(principal, "run.write")
        if idempotency_key is not None and len(idempotency_key) > 255:
            raise ValueError("idempotency key exceeds 255 characters")
        parts = _domain_content(content)
        wire_body = {"content": _wire_content(content)}
        request_hash = hashlib.sha256(canonical_json(wire_body).encode("utf-8")).hexdigest()
        now = self._clock.now()
        dispatch = False
        try:
            async with self._uow_factory() as uow:
                session = await uow.sessions.get(session_id, principal)
                if session.status is SessionStatus.CLOSED:
                    raise InvalidStateTransition("closed sessions cannot accept messages")
                if idempotency_key is not None:
                    # The PostgreSQL adapter takes a transaction-scoped advisory
                    # lock here. A concurrent same-key request therefore observes
                    # the committed record before it can enter either run path.
                    existing = await uow.idempotency.get(
                        idempotency_key, principal.tenant_id, principal.principal_id
                    )
                    if existing is not None:
                        original = await uow.runs.get(existing.run_id, principal)
                        # The normative hash excludes the path. Run ownership still
                        # makes reuse on another session an explicit conflict.
                        if (
                            existing.request_hash != request_hash
                            or original.session_id != session_id
                        ):
                            raise ConflictError(
                                "The idempotency key was reused with a different body.",
                                reason="idempotency_key_reused",
                            )
                        raise _ExistingRunError(original)
                active = await uow.runs.active_for_session(session_id, principal)
                if active is not None:
                    if active.status is RunStatus.WAITING_FOR_USER:
                        result = await self._deliver_input_in(uow, principal, active, content, None)
                        if idempotency_key is not None:
                            await uow.idempotency.create(
                                IdempotencyRecord(
                                    key=idempotency_key,
                                    tenant_id=principal.tenant_id,
                                    principal_id=principal.principal_id,
                                    request_hash=request_hash,
                                    run_id=active.id,
                                    created_at=now,
                                    expires_at=now + timedelta(hours=24),
                                )
                            )
                        dispatch = True
                        run = active.model_copy(update={"status": result.status})
                    else:
                        raise ConflictError(
                            "The session already has an active run.",
                            reason="active_run_exists",
                            details={
                                "run_id": str(active.id),
                                "run_status": active.status.value,
                            },
                        )
                else:
                    if session.title is None and (
                        await uow.runs.latest_for_session(session.id, principal) is None
                    ):
                        title = next(
                            (
                                candidate
                                for block in content
                                if isinstance(block, TextContentBlock)
                                if (candidate := conversation_title(block.text)) is not None
                            ),
                            None,
                        )
                        if title is not None:
                            session = await uow.sessions.set_title_if_missing(
                                session.id, principal, title
                            )
                    agent = await uow.agents.get_version(session.agent_id, session.agent_version)
                    consent = await uow.export_consent.get(
                        principal.tenant_id, principal.principal_id
                    )
                    run = Run(
                        id=self._ids.new_id(),
                        session_id=session.id,
                        tenant_id=session.tenant_id,
                        principal_scopes=set(principal.scopes),
                        agent_id=session.agent_id,
                        agent_version=session.agent_version,
                        status=RunStatus.QUEUED,
                        limits=agent.limits.model_copy(deep=True),
                        priority=0,
                        scheduled_for=now,
                        deadline_at=agent.limits.deadline_at,
                        export_consent=(
                            self._trajectory_export_enabled
                            and consent is not None
                            and consent.active
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
                            session_id=session.id,
                            run_id=run.id,
                            event_type="user.message.created",
                            actor_type="principal",
                            actor_id=principal.principal_id,
                            payload={"content": [part.model_dump(mode="json") for part in parts]},
                            trace_id=trace_id,
                        )
                    )
                    await uow.runs.set_seed_event_sequence(run.id, user_event.sequence)
                    await uow.events.append(
                        NewEvent(
                            session_id=session.id,
                            run_id=run.id,
                            event_type="run.queued",
                            actor_type="application",
                            payload={"run_id": str(run.id), "priority": run.priority},
                            trace_id=trace_id,
                        )
                    )
                    await self._seed_checkpoint(
                        uow,
                        run,
                        user_event.sequence,
                        None,
                        principal,
                    )
                    if idempotency_key is not None:
                        record = await uow.idempotency.create(
                            IdempotencyRecord(
                                key=idempotency_key,
                                tenant_id=principal.tenant_id,
                                principal_id=principal.principal_id,
                                request_hash=request_hash,
                                run_id=run.id,
                                created_at=now,
                                expires_at=now + timedelta(hours=24),
                            )
                        )
                        if record.run_id != run.id:
                            original = await uow.runs.get(record.run_id, principal)
                            raise _ExistingRunError(original)
                    dispatch = True
        except _ExistingRunError as duplicate:
            return SubmitResult(
                run_id=duplicate.run.id,
                status=duplicate.run.status,
                replayed=True,
            )
        if dispatch:
            if active is None:
                await self._dispatcher.dispatch(run.id)
            else:
                await self._dispatcher.resume(run.id)
        return SubmitResult(run_id=run.id, status=run.status)

    async def get(self, principal: Principal, run_id: UUID) -> RunView:
        require_scope(principal, "run.read")
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, principal)
        return _run_view(run)

    async def cancel(self, principal: Principal, run_id: UUID) -> CancelResult:
        require_scope(principal, "run.cancel")
        accepted = False
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, principal)
            if run.status in TERMINAL_RUN_STATUSES:
                return CancelResult(run=_run_view(run), accepted=False)
            if run.status in {
                RunStatus.QUEUED,
                RunStatus.WAITING_FOR_APPROVAL,
                RunStatus.WAITING_FOR_USER,
            }:
                try:
                    run = await self._cancel_parked_run(uow, run, principal.principal_id)
                except ConflictError:
                    run = await uow.runs.get(run_id, principal)
                    if run.status is not RunStatus.RUNNING:
                        if run.status in TERMINAL_RUN_STATUSES:
                            return CancelResult(run=_run_view(run), accepted=False)
                        raise
                else:
                    return CancelResult(run=_run_view(run), accepted=False)
            if run.status is RunStatus.RUNNING:
                run = await uow.runs.request_cancellation(run.id, RunStatus.RUNNING)
                accepted = True
        if accepted:
            self._cancel_active(run_id)
        return CancelResult(run=_run_view(run), accepted=accepted)

    async def _events_after(
        self, principal: Principal, run_id: UUID, after_sequence: int
    ) -> tuple[Run, list[EventEnvelope]]:
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, principal)
            events = await uow.events.list_after(run.session_id, after_sequence, principal)
        return run, [event for event in events if event.run_id == run.id]

    async def stream(
        self,
        principal: Principal,
        run_id: UUID,
        after_sequence: int | None,
    ) -> AsyncIterator[StreamFrame]:
        require_scope(principal, "run.read")
        watermark = max(0, after_sequence or 0)
        run = await self.get(principal, run_id)
        async with self._live_events.subscribe(run.session_id) as subscription:
            while True:
                current, events = await self._events_after(principal, run_id, watermark)
                for event in events:
                    watermark = max(watermark, event.sequence)
                    yield PersistedStreamFrame(
                        sequence=event.sequence,
                        event=event.event_type,
                        data={"run_id": str(current.id), **event.payload},
                    )
                if current.status in TERMINAL_RUN_STATUSES:
                    return
                if subscription.overflowed:
                    yield TransientStreamFrame(
                        event="stream.overflow", data={"last_sequence": watermark}
                    )
                    return
                notification = await subscription.receive(_IDLE_POLL_SECONDS)
                if subscription.overflowed:
                    yield TransientStreamFrame(
                        event="stream.overflow", data={"last_sequence": watermark}
                    )
                    return
                if (
                    notification is not None
                    and notification.kind == "transient"
                    and notification.run_id == run_id
                    and notification.event is not None
                ):
                    yield TransientStreamFrame(
                        event=notification.event,
                        data=dict(notification.data or {}),
                    )

    async def _deliver_input_in(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        run: Run,
        content: list[ContentBlock],
        question_id: UUID | None,
    ) -> SubmitResult:
        checkpoint = await uow.checkpoints.latest(run.id)
        if checkpoint is None:
            raise InvalidStateTransition("waiting run has no checkpoint")
        outstanding = checkpoint.working_state.get("outstanding_question_id")
        try:
            outstanding_id = None if outstanding is None else UUID(str(outstanding))
        except (TypeError, ValueError) as exc:
            raise InvalidStateTransition(
                "checkpoint has no valid outstanding question identifier"
            ) from exc
        invocations = await uow.invocations.list_for_run(run.id, principal)
        suspended = [
            invocation
            for invocation in invocations
            if invocation.status is ToolInvocationStatus.RUNNING
            and invocation.suspended_kind == "user_input"
        ]
        if len(suspended) != 1:
            raise InvalidStateTransition("waiting run does not have exactly one suspended question")
        invocation = suspended[0]
        try:
            invocation_question = UUID(str(invocation.suspended_ref))
        except (TypeError, ValueError) as exc:
            raise InvalidStateTransition(
                "suspended invocation has no valid question identifier"
            ) from exc
        if outstanding_id is None:
            outstanding_id = invocation_question
        elif outstanding_id != invocation_question:
            raise ConflictError("checkpoint and invocation question identifiers disagree")
        effective_question = question_id or outstanding_id
        if effective_question != outstanding_id:
            raise ConflictError("the question was already resolved")
        parts = _domain_content(content)
        await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal.principal_id,
                payload={
                    "content": [part.model_dump(mode="json") for part in parts],
                    "question_id": str(effective_question),
                },
                derivation_key=f"run.input:{run.id}:{effective_question}",
            )
        )
        outcome = ToolOutcome(
            status=ToolOutcomeStatus.SUCCEEDED,
            action=invocation.tool_name,
            reason_code="tool.succeeded",
            message="The user answered the outstanding question.",
            retryable=False,
            remediation="none",
        )
        result_item = ToolResultItem(
            call_id=invocation.call_id,
            content=parts,
            trust=TrustLevel.USER,
        )
        completed = invocation.model_copy(
            update={
                "status": ToolInvocationStatus.SUCCEEDED,
                "suspended_kind": None,
                "suspended_ref": None,
                "outcome": outcome,
                "structured_result": {
                    "question_id": str(effective_question),
                    "answered": True,
                },
                "result_item": result_item,
                "updated_at": self._clock.now(),
            },
            deep=True,
        )
        await uow.invocations.transition(
            invocation.id,
            ToolInvocationStatus.RUNNING,
            completed,
        )
        completed_event = await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="tool.call.completed",
                actor_type="application",
                actor_id=principal.principal_id,
                payload={
                    "name": invocation.tool_name,
                    "call_id": invocation.call_id,
                    "reason_code": outcome.reason_code,
                    "result_item": result_item.model_dump(mode="json"),
                },
                derivation_key=f"run.input.completed:{run.id}:{effective_question}",
            )
        )
        checkpoint.version += 1
        checkpoint.status = RunStatus.QUEUED
        question_text = checkpoint.working_state.get("outstanding_question_text")
        raw_state = checkpoint.working_state.get("context")
        state = WorkingState() if raw_state is None else WorkingState.model_validate(raw_state)
        state = self._resolve_open_question(
            state,
            question_text if isinstance(question_text, str) else None,
        )
        checkpoint.working_state["context"] = state.model_dump(mode="json")
        checkpoint.working_state.pop("outstanding_question_id", None)
        checkpoint.working_state.pop("outstanding_question_text", None)
        state_event = await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="context.working_state.updated",
                actor_type="application",
                actor_id=principal.principal_id,
                payload={
                    "working_state": state.model_dump(mode="json"),
                    "source": "user_answer",
                },
                derivation_key=f"run.input.state:{run.id}:{effective_question}",
            )
        )
        checkpoint.last_event_sequence = max(completed_event.sequence, state_event.sequence)
        checkpoint.created_at = self._clock.now()
        await uow.checkpoints.write(run.id, checkpoint, full=True)
        await self._resume_waiting_run(uow, run)
        return SubmitResult(run_id=run.id, status=RunStatus.QUEUED)

    async def deliver_input(
        self,
        principal: Principal,
        run_id: UUID,
        content: list[ContentBlock],
        question_id: UUID | None,
    ) -> SubmitResult:
        require_scope(principal, "run.write")
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, principal)
            replay = (
                None
                if question_id is None
                else await uow.events.get_by_derivation(
                    f"run.input:{run.id}:{question_id}", principal
                )
            )
            if replay is not None:
                return SubmitResult(run_id=run.id, status=run.status, replayed=True)
            if run.status is not RunStatus.WAITING_FOR_USER:
                raise InvalidStateTransition(
                    "run is not waiting for user input",
                    details={"run_status": run.status.value},
                )
            result = await self._deliver_input_in(uow, principal, run, content, question_id)
        await self._dispatcher.resume(run.id)
        return result


def _encode_cursor(row: ApprovalRequest) -> str:
    payload = json.dumps(
        {"k": row.created_at.isoformat(), "i": str(row.id)}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str | None) -> ApprovalCursor | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"k", "i"}:
            raise ValueError
        return ApprovalCursor(created_at=datetime.fromisoformat(raw["k"]), id=UUID(raw["i"]))
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("approval cursor is malformed") from exc


class PublicApprovalService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        dispatcher: RunDispatcher,
        resume_waiting_run: ResumeWaitingRun,
        self_approval_enabled: bool,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher
        self._resume_waiting_run = resume_waiting_run
        self._self_approval_enabled = self_approval_enabled

    async def list(
        self,
        principal: Principal,
        filters: ApprovalFilters,
        limit: int,
        cursor: str | None,
    ) -> Page[ApprovalView]:
        require_scope(principal, "approval.read")
        effective_limit = min(max(limit, 1), 200)
        decoded = _decode_cursor(cursor)
        async with self._uow_factory() as uow:
            if filters.run_id is not None:
                await uow.runs.get(filters.run_id, principal)
            if filters.session_id is not None:
                await uow.sessions.get(filters.session_id, principal)
            rows = await uow.approvals.list_pending(
                principal,
                run_id=filters.run_id,
                session_id=filters.session_id,
                limit=effective_limit + 1,
                cursor=decoded,
            )
        has_more = len(rows) > effective_limit
        page_rows = rows[:effective_limit]
        return Page[ApprovalView](
            items=[_approval_view(row) for row in page_rows],
            next_cursor=_encode_cursor(page_rows[-1]) if has_more and page_rows else None,
        )

    async def get(self, principal: Principal, approval_id: UUID) -> ApprovalView:
        require_scope(principal, "approval.read")
        async with self._uow_factory() as uow:
            approval = await uow.approvals.get(approval_id, principal)
        return _approval_view(approval)

    async def resolve(
        self,
        principal: Principal,
        approval_id: UUID,
        decision: ApprovalResolutionType,
        reason: str | None,
    ) -> ApprovalView:
        require_scope(principal, "approval.resolve")
        dispatch_run: UUID | None = None
        async with self._uow_factory() as uow:
            visible = await uow.approvals.get(approval_id, principal)
            if visible.status in {ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED}:
                raise ConflictError(
                    "The approval is no longer pending.",
                    details={"status": visible.status.value},
                )
            if not self._self_approval_enabled and visible.principal_id == principal.principal_id:
                raise AuthorizationError("approval requires a distinct resolver")
            outcome = await uow.approvals.resolve(approval_id, principal, decision, reason)
            if outcome.state is ApprovalResolutionState.ALREADY_RESOLVED_DIFFERENTLY:
                raise ConflictError(
                    "The approval was already resolved differently.",
                    reason="approval_already_resolved",
                    details={
                        "approval_id": str(approval_id),
                        "decision": (
                            None
                            if outcome.approval.resolution is None
                            else outcome.approval.resolution.value
                        ),
                    },
                )
            if outcome.state is ApprovalResolutionState.APPLIED:
                owner = Principal(
                    tenant_id=outcome.approval.tenant_id,
                    principal_id=outcome.approval.principal_id,
                )
                run = await uow.runs.get(outcome.approval.run_id, owner)
                await uow.events.append(
                    NewEvent(
                        session_id=run.session_id,
                        run_id=run.id,
                        event_type="approval.resolved",
                        actor_type="principal",
                        actor_id=principal.principal_id,
                        payload={
                            "approval_id": str(outcome.approval.id),
                            "resolution": decision.value,
                        },
                    )
                )
                if run.status is RunStatus.WAITING_FOR_APPROVAL:
                    await self._resume_waiting_run(uow, run)
                    dispatch_run = run.id
        if dispatch_run is not None:
            await self._dispatcher.resume(dispatch_run)
        return _approval_view(outcome.approval)


class PublicArtifactService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifacts: TrajectoryArtifactStore,
        general_artifacts: ArtifactStore,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifacts = artifacts
        self._general_artifacts = general_artifacts
        self._clock = clock

    async def _get_ref(self, principal: Principal, artifact_id: UUID) -> tuple[ArtifactRef, bool]:
        async with self._uow_factory() as uow:
            try:
                artifact = await uow.trajectory_exports.get_artifact(artifact_id, principal)
                is_trajectory = True
            except NotFoundError:
                artifact = await uow.artifacts.get(artifact_id, principal)
                is_trajectory = False
        # ArtifactRef requires an expiry, and persistence mapping rejects legacy null rows.
        if artifact.expires_at is not None and artifact.expires_at <= self._clock.now():
            raise NotFoundError("artifact not found")
        return artifact, is_trajectory

    async def get(self, principal: Principal, artifact_id: UUID) -> ArtifactView:
        require_scope(principal, "artifact.read")
        artifact, _is_trajectory = await self._get_ref(principal, artifact_id)
        return _artifact_view(artifact)

    async def open_content(self, principal: Principal, artifact_id: UUID) -> ArtifactContent:
        require_scope(principal, "artifact.read")
        artifact, is_trajectory = await self._get_ref(principal, artifact_id)

        async def open_stream() -> AsyncIterator[bytes]:
            try:
                if is_trajectory:
                    return await self._artifacts.open_verified(artifact)
                ref = StoredArtifactRef(
                    artifact_id=artifact.id,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.media_type,
                )
                return await self._general_artifacts.open_verified(
                    ref, tenant_id=artifact.tenant_id
                )
            except FileNotFoundError as exc:
                raise NotFoundError("artifact not found") from exc

        return ArtifactContent(artifact=_artifact_view(artifact), open=open_stream)
