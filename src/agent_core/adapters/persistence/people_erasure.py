"""Selective removal of generated People copies, preserving original owner input."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import BigInteger, Select, Text, any_, bindparam, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.people_reference_index import (
    INVOCATION_REFERENCE_TEXT,
    reference_overlap,
)
from agent_core.adapters.persistence.sqlalchemy_models import (
    ArtifactRow,
    CheckpointRow,
    EventRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    RecallTraceRow,
    RunRow,
    SessionHistoryItemRow,
    SessionRow,
    ToolInvocationRow,
    TrajectoryProjectionRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.people import PeopleCopyCleanup, PeopleErasure
from agent_core.domain.runs import TERMINAL_RUN_STATUSES

MARKER = "[People memory erased]"
ORIGINAL_EVENTS = frozenset({"user.message.created", "people.owner_assertion"})


def email_cleanup_key(principal: Principal, account_id: str, thread_id: str) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                "email-people-cleanup@1",
                principal.tenant_id,
                principal.principal_id,
                account_id,
                thread_id,
            ]
        ).encode()
    ).hexdigest()


def source_cleanup_receipts(
    principal: Principal,
    source_ids: Sequence[UUID],
    copy_ids: Sequence[UUID],
    cleanup: PeopleCopyCleanup,
    erased_at: datetime,
    *,
    belief_ids: Sequence[UUID] = (),
    request_hash: str | None = None,
) -> list[PeopleErasure]:
    """Independent bounded receipts let ordinary source deletion resume cleanup."""
    if not source_ids:
        return []
    sources, copies, runs = sorted(set(source_ids)), sorted(set(copy_ids)), cleanup.pending_run_ids
    beliefs = sorted(set(belief_ids))
    digest = hashlib.sha256(
        (
            f"{principal.tenant_id}/{principal.principal_id}/{erased_at.isoformat()}/"
            + "/".join(str(key) for key in sources)
        ).encode()
    ).hexdigest()
    root = uuid5(NAMESPACE_URL, f"people-source-cleanup:{digest}")
    receipts = []
    # Reserve one slot for the shared source identity in every independent root.
    for index, start in enumerate(
        range(0, max(len(sources), len(copies), len(runs), len(beliefs), 1), 255)
    ):
        artifacts = cleanup.artifact_ids if index == 0 else []
        pending_generated = cleanup.pending_generated and index == 0
        pending_runs = runs[start : start + 255]
        receipts.append(
            PeopleErasure(
                id=uuid5(root, str(index)),
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                created_at=erased_at,
                updated_at=erased_at,
                target_id=sources[0],
                expected_revisions={},
                expires_at=erased_at,
                request_hash=request_hash or digest,
                blocked_source_ids=sorted({sources[0], *sources[start : start + 255]}),
                blocked_record_ids=sorted({sources[0], *copies[start : start + 255]}),
                blocked_belief_ids=beliefs[start : start + 255],
                pending_run_ids=pending_runs,
                pending_artifact_ids=artifacts,
                pending_generated=pending_generated,
                state="cleanup_pending"
                if pending_generated or pending_runs or artifacts
                else "completed",
                counts=cleanup.counts if index == 0 else {},
            )
        )
    return receipts


async def lock_run_erasure(session: AsyncSession, run_id: UUID) -> datetime | None:
    """Serialize generated-copy writes against the run's permanent erasure fence."""
    return await session.scalar(
        select(RunRow.people_erased_at).where(RunRow.id == run_id).with_for_update()
    )


def references(value: Any, identifiers: set[str]) -> bool:
    if isinstance(value, str):
        return any(key in value for key in identifiers)
    if isinstance(value, dict):
        return any(references(item, identifiers) for item in value.values())
    if isinstance(value, list):
        return any(references(item, identifiers) for item in value)
    return False


def redact(value: Any) -> Any:
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "raw_arguments":
            result[key] = "{}"
        elif key in {
            "arguments",
            "normalized_arguments",
            "modified_arguments",
            "structured",
            "structured_result",
            "data",
            "working_state",
            "details",
        }:
            result[key] = {} if item is not None else None
        elif key in {
            "content",
            "text",
            "statement",
            "subject",
            "display_name",
            "narrative",
            "final_message",
            "summary",
            "compacted_summary",
            "memory_snapshot",
            "reason",
            "explanation",
            "message",
            "error",
            "rendered",
        }:
            if isinstance(item, list):
                result[key] = [{"type": "text", "text": MARKER}]
            elif isinstance(item, dict):
                result[key] = redact(item)
            else:
                result[key] = MARKER if item is not None else None
        else:
            result[key] = redact(item)
    return result


async def erase_postgres_copies(
    session: AsyncSession,
    principal: Principal,
    record_ids: list[UUID],
    erased_at: datetime,
    *,
    run_ids: Sequence[UUID] = (),
    purge_generated: bool = True,
) -> PeopleCopyCleanup:
    sessions = select(SessionRow.id).where(
        SessionRow.tenant_id == principal.tenant_id,
        SessionRow.principal_id == principal.principal_id,
    )
    events = select(EventRow).where(
        EventRow.session_id.in_(sessions),
        EventRow.event_type.not_in(ORIGINAL_EVENTS),
    )
    invocations = select(ToolInvocationRow).where(ToolInvocationRow.session_id.in_(sessions))
    changed_events: set[int] = set()
    changed_invocations: set[UUID] = set()
    runs = set(run_ids)
    if record_ids:
        seen = set(record_ids)
        frontier = set(record_ids)
        expanded_runs: set[UUID] = set()
        while frontier:
            # Follow opaque IDs through generated artifacts, knowledge, and
            # downstream recall. No names or unrelated payloads are loaded.
            discovered: set[UUID] = set()
            plan_sessions: set[UUID] = set()
            for event_id, run_id, session_id, event_type in await session.execute(
                events.with_only_columns(
                    EventRow.id, EventRow.run_id, EventRow.session_id, EventRow.event_type
                ).where(reference_overlap("payload::text", list(frontier)))
            ):
                changed_events.add(event_id)
                if run_id is not None:
                    runs.add(run_id)
                if event_type in {"context.plan.created", "context.epoch.rotated"}:
                    plan_sessions.add(session_id)
            if plan_sessions:
                runs.update(
                    (
                        await session.scalars(
                            select(RunRow.id).where(
                                RunRow.session_id
                                == any_(
                                    bindparam(
                                        None, list(plan_sessions), type_=ARRAY(PGUUID(as_uuid=True))
                                    )
                                ),
                            )
                        )
                    ).all()
                )
            for invocation_id, run_id in await session.execute(
                invocations.with_only_columns(ToolInvocationRow.id, ToolInvocationRow.run_id).where(
                    reference_overlap(INVOCATION_REFERENCE_TEXT, list(frontier))
                )
            ):
                changed_invocations.add(invocation_id)
                runs.add(run_id)
            for trace_id, run_id in await session.execute(
                select(RecallTraceRow.id, RecallTraceRow.trace["run_id"].astext).where(
                    RecallTraceRow.tenant_id == principal.tenant_id,
                    RecallTraceRow.principal_id == principal.principal_id,
                    reference_overlap("trace::text", list(frontier)),
                )
            ):
                discovered.add(trace_id)
                if run_id is not None:
                    runs.add(UUID(run_id))
            runs = set(
                (
                    await session.scalars(
                        select(RunRow.id).where(
                            RunRow.id
                            == any_(bindparam(None, list(runs), type_=ARRAY(PGUUID(as_uuid=True)))),
                            RunRow.session_id.in_(sessions),
                        )
                    )
                ).all()
            )
            new_runs = runs - expanded_runs
            artifacts = select(ArtifactRow.id).where(
                ArtifactRow.run_id
                == any_(bindparam(None, list(new_runs), type_=ARRAY(PGUUID(as_uuid=True)))),
                ArtifactRow.origin != "upload",
            )
            discovered.update((await session.scalars(artifacts)).all())
            discovered.update(
                (
                    await session.scalars(
                        select(KnowledgeDocumentRow.document_id).where(
                            KnowledgeDocumentRow.source_artifact_id.in_(artifacts),
                        )
                    )
                ).all()
            )
            expanded_runs.update(new_runs)
            frontier = discovered - seen
            seen.update(discovered)
        record_ids = list(seen)
    runs = set(
        (
            await session.scalars(
                select(RunRow.id).where(
                    RunRow.id
                    == any_(bindparam(None, list(runs), type_=ARRAY(PGUUID(as_uuid=True)))),
                    RunRow.session_id.in_(sessions),
                )
            )
        ).all()
    )
    run_keys = any_(bindparam(None, list(runs), type_=ARRAY(PGUUID(as_uuid=True))))
    event_keys = any_(bindparam(None, list(changed_events), type_=ARRAY(BigInteger())))
    invocation_keys = any_(
        bindparam(None, list(changed_invocations), type_=ARRAY(PGUUID(as_uuid=True)))
    )
    active = list(
        (
            await session.scalars(
                select(RunRow.id).where(
                    RunRow.id == run_keys,
                    RunRow.status.not_in([status.value for status in TERMINAL_RUN_STATUSES]),
                )
            )
        ).all()
    )
    if active:
        await session.execute(
            update(RunRow)
            .where(RunRow.id == any_(bindparam(None, active, type_=ARRAY(PGUUID(as_uuid=True)))))
            .values(
                cancel_requested_at=erased_at,
            )
        )
    await session.execute(
        update(RunRow)
        .where(RunRow.id == run_keys, RunRow.people_erased_at.is_(None))
        .values(people_erased_at=erased_at, erasure_pending=True)
    )
    # Fence metadata atomically; rewrite content only in resumable pages.
    await session.execute(
        update(EventRow)
        .where(
            EventRow.session_id.in_(sessions),
            EventRow.event_type.not_in(ORIGINAL_EVENTS),
            ~EventRow.people_erased,
            or_(EventRow.id == event_keys, EventRow.run_id == run_keys),
        )
        .values(people_erased=True, erasure_pending=True)
    )
    await session.execute(
        update(ToolInvocationRow)
        .where(
            ToolInvocationRow.session_id.in_(sessions),
            ~ToolInvocationRow.people_erased,
            or_(ToolInvocationRow.id == invocation_keys, ToolInvocationRow.run_id == run_keys),
        )
        .values(people_erased=True, erasure_pending=True)
    )
    await session.execute(
        update(RecallTraceRow)
        .where(
            RecallTraceRow.tenant_id == principal.tenant_id,
            RecallTraceRow.principal_id == principal.principal_id,
            ~RecallTraceRow.erasure_pending,
            or_(
                reference_overlap("trace::text", record_ids),
                RecallTraceRow.trace["run_id"].astext
                == any_(bindparam(None, [str(key) for key in runs], type_=ARRAY(Text()))),
            ),
        )
        .values(erasure_pending=True)
    )
    generated_counts, pending_generated = await _purge_generated_page(
        session, sessions, principal, limit=256 if purge_generated else 0
    )
    await session.execute(
        delete(TrajectoryProjectionRow).where(TrajectoryProjectionRow.run_id == run_keys)
    )
    artifact_scope = (ArtifactRow.run_id == run_keys, ArtifactRow.origin != "upload")
    await session.execute(
        update(ArtifactRow)
        .where(*artifact_scope)
        .values(
            expires_at=func.least(func.coalesce(ArtifactRow.expires_at, erased_at), erased_at),
        )
    )
    artifact_count = (
        await session.scalar(select(func.count()).select_from(ArtifactRow).where(*artifact_scope))
        or 0
    )
    artifact_ids = list(
        (
            await session.scalars(
                select(ArtifactRow.id).where(*artifact_scope).order_by(ArtifactRow.id).limit(256)
            )
        ).all()
    )
    return PeopleCopyCleanup(
        pending_generated=pending_generated,
        active_run_ids=active,
        pending_run_ids=sorted(runs) if active or artifact_count > 256 else [],
        artifact_ids=artifact_ids,
        counts={
            "active_runs": len(active),
            **generated_counts,
            "pending_artifacts": artifact_count,
        },
    )


async def _purge_generated_page(
    session: AsyncSession,
    sessions: Select[tuple[UUID]],
    principal: Principal,
    *,
    limit: int,
) -> tuple[dict[str, int], bool]:
    """At most 256 payloads of each kind; readers honor the durable fences."""
    runs = select(RunRow).where(RunRow.session_id.in_(sessions), RunRow.erasure_pending)
    run_page = list((await session.scalars(runs.order_by(RunRow.id).limit(limit))).all())
    for run in run_page:
        run.final_message = None
        run.failure = None
        run.erasure_pending = False
    events = select(EventRow).where(
        EventRow.session_id.in_(sessions),
        EventRow.erasure_pending,
    )
    event_page = list((await session.scalars(events.order_by(EventRow.id).limit(limit))).all())
    for row in event_page:
        row.payload = redact(row.payload)
        row.erasure_pending = False
    invocations = select(ToolInvocationRow).where(
        ToolInvocationRow.session_id.in_(sessions),
        ToolInvocationRow.erasure_pending,
    )
    invocation_page = list(
        (await session.scalars(invocations.order_by(ToolInvocationRow.id).limit(limit))).all()
    )
    for invocation in invocation_page:
        for field in (
            "raw_arguments",
            "arguments",
            "result_item",
            "structured_result",
            "outcome",
            "policy_decision",
        ):
            setattr(invocation, field, redact({field: getattr(invocation, field)})[field])
        invocation.erasure_pending = False
    histories = (
        select(SessionHistoryItemRow)
        .join(
            EventRow,
            (EventRow.session_id == SessionHistoryItemRow.session_id)
            & (EventRow.sequence == SessionHistoryItemRow.sequence),
        )
        .where(
            SessionHistoryItemRow.session_id.in_(sessions),
            EventRow.people_erased,
            ~SessionHistoryItemRow.erasure_cleaned,
        )
    )
    history_page = list(
        (
            await session.scalars(
                histories.order_by(
                    SessionHistoryItemRow.session_id,
                    SessionHistoryItemRow.sequence,
                    SessionHistoryItemRow.item_index,
                ).limit(limit)
            )
        ).all()
    )
    for history in history_page:
        history.item = redact(history.item)
        history.erasure_cleaned = True
    checkpoint_ids = (
        select(CheckpointRow.id)
        .join(
            RunRow,
            RunRow.id == CheckpointRow.run_id,
        )
        .where(RunRow.session_id.in_(sessions), RunRow.people_erased_at.is_not(None))
    )
    removed = list(
        (
            await session.scalars(
                delete(CheckpointRow)
                .where(CheckpointRow.id.in_(checkpoint_ids.order_by(CheckpointRow.id).limit(limit)))
                .returning(CheckpointRow.id)
            )
        ).all()
    )
    traces = select(RecallTraceRow.id).where(
        RecallTraceRow.tenant_id == principal.tenant_id,
        RecallTraceRow.principal_id == principal.principal_id,
        RecallTraceRow.erasure_pending,
    )
    removed_traces = list(
        (
            await session.scalars(
                delete(RecallTraceRow)
                .where(RecallTraceRow.id.in_(traces.order_by(RecallTraceRow.id).limit(limit)))
                .returning(RecallTraceRow.id)
            )
        ).all()
    )
    documents = (
        select(KnowledgeDocumentRow.row_id)
        .join(
            ArtifactRow,
            ArtifactRow.id == KnowledgeDocumentRow.source_artifact_id,
        )
        .join(RunRow, RunRow.id == ArtifactRow.run_id)
        .where(
            RunRow.session_id.in_(sessions),
            RunRow.people_erased_at.is_not(None),
            ArtifactRow.origin != "upload",
        )
    )
    chunks = select(KnowledgeChunkRow.chunk_id).where(
        KnowledgeChunkRow.document_row_id.in_(documents),
    )
    removed_chunks = list(
        (
            await session.scalars(
                delete(KnowledgeChunkRow)
                .where(
                    KnowledgeChunkRow.chunk_id.in_(
                        chunks.order_by(KnowledgeChunkRow.chunk_id).limit(limit)
                    )
                )
                .returning(KnowledgeChunkRow.chunk_id)
            )
        ).all()
    )
    empty_documents = documents.where(
        ~select(KnowledgeChunkRow.chunk_id)
        .where(
            KnowledgeChunkRow.document_row_id == KnowledgeDocumentRow.row_id,
        )
        .exists()
    )
    removed_documents = list(
        (
            await session.scalars(
                delete(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.row_id.in_(
                        empty_documents.order_by(KnowledgeDocumentRow.row_id).limit(limit)
                    )
                )
                .returning(KnowledgeDocumentRow.row_id)
            )
        ).all()
    )
    await session.flush()
    pending = any(
        [
            bool(await session.scalar(select(events.exists()))),
            bool(await session.scalar(select(runs.exists()))),
            bool(await session.scalar(select(invocations.exists()))),
            bool(await session.scalar(select(histories.exists()))),
            bool(await session.scalar(select(checkpoint_ids.exists()))),
            bool(await session.scalar(select(traces.exists()))),
            bool(await session.scalar(select(documents.exists()))),
        ]
    )
    return {
        "events": len(event_page),
        "run_messages": len(run_page),
        "invocations": len(invocation_page),
        "histories": len(history_page),
        "checkpoints": len(removed),
        "traces": len(removed_traces),
        "knowledge_chunks": len(removed_chunks),
        "knowledge_documents": len(removed_documents),
    }, pending


def erase_memory_copies_locked(
    repository: Any,
    principal: Principal,
    record_ids: list[UUID],
    erased_at: datetime,
    *,
    run_ids: Sequence[UUID] = (),
) -> PeopleCopyCleanup:
    identifiers = {str(key) for key in record_ids}
    sessions = {
        key
        for key, value in repository._sessions._sessions.items()
        if (value.tenant_id, value.principal_id) == (principal.tenant_id, principal.principal_id)
    }
    events = [event for sid in sessions for event in repository._events._events.get(sid, [])]
    invocations = [
        row for row in repository._invocations._invocations.values() if row.session_id in sessions
    ]
    changed_events: set[int] = set()
    changed_invocations: set[UUID] = set()
    runs = set(run_ids)
    while True:
        changed_events.update(
            row.id
            for row in events
            if row.event_type not in ORIGINAL_EVENTS and references(row.payload, identifiers)
        )
        changed_invocations.update(
            row.id for row in invocations if references(row.model_dump(mode="json"), identifiers)
        )
        runs.update(
            row.run_id for row in events if row.id in changed_events and row.run_id is not None
        )
        runs.update(row.run_id for row in invocations if row.id in changed_invocations)
        plan_sessions = {
            row.session_id
            for row in events
            if row.id in changed_events
            and row.event_type in {"context.plan.created", "context.epoch.rotated"}
        }
        runs.update(
            row.id for row in repository._runs._runs.values() if row.session_id in plan_sessions
        )
        matched_traces = [
            trace
            for trace in repository._traces._traces.values()
            if (trace.tenant_id, trace.principal_id)
            == (principal.tenant_id, principal.principal_id)
            and references(trace.model_dump(mode="json"), identifiers)
        ]
        runs.update(trace.run_id for trace in matched_traces if trace.run_id is not None)
        runs = {
            key
            for key in runs
            if key in repository._runs._runs and repository._runs._runs[key].session_id in sessions
        }
        discovered_artifact_ids = {
            artifact.id
            for artifact in repository._artifacts._rows.values()
            if artifact.run_id in runs and artifact.origin != "upload"
        }
        discovered = {str(key) for key in discovered_artifact_ids}
        discovered.update(str(trace.id) for trace in matched_traces)
        discovered.update(
            str(document.document_id)
            for document in repository._knowledge._documents.values()
            if document.source_ref.id in discovered_artifact_ids
        )
        if not discovered - identifiers:
            break
        identifiers.update(discovered)
    repository._traces._traces = {
        key: trace
        for key, trace in repository._traces._traces.items()
        if not (
            (trace.tenant_id, trace.principal_id) == (principal.tenant_id, principal.principal_id)
            and (trace.run_id in runs or references(trace.model_dump(mode="json"), identifiers))
        )
    }
    active = [
        row.id
        for row in repository._runs._runs.values()
        if row.id in runs and row.status not in TERMINAL_RUN_STATUSES
    ]
    if active:
        for run_id in active:
            repository._runs._runs[run_id] = repository._runs._runs[run_id].model_copy(
                update={"cancel_requested_at": erased_at},
            )
    for repo in (
        repository._events,
        repository._runs,
        repository._invocations,
        repository._checkpoints,
        repository._artifacts,
        repository._trajectory_exports,
        repository._knowledge,
        repository._traces,
    ):
        for key in runs:
            repo._people_erased_runs.setdefault(key, erased_at)
    count = 0
    for sid in sessions:
        updated = []
        for event in repository._events._events.get(sid, []):
            if event.event_type not in ORIGINAL_EVENTS and (
                event.id in changed_events or event.run_id in runs
            ):
                event = event.model_copy(update={"payload": redact(event.payload)}, deep=True)
                count += 1
            updated.append(event)
            if event.derivation_key:
                repository._events._derived[event.derivation_key] = event
        repository._events._events[sid] = updated
    for key, row in list(repository._invocations._invocations.items()):
        if row.id in changed_invocations or row.run_id in runs:
            repository._invocations._invocations[key] = type(row).model_validate(
                redact(row.model_dump(mode="json"))
            )
    checkpoints = sum(len(repository._checkpoints._checkpoints.pop(run_id, [])) for run_id in runs)
    for run_id in runs:
        if run_id in repository._runs._runs:
            repository._runs._runs[run_id] = repository._runs._runs[run_id].model_copy(
                update={"final_message": None, "failure": None}
            )
    artifact_ids: set[UUID] = set()
    for key, artifact in list(repository._artifacts._rows.items()):
        if artifact.run_id in runs and artifact.origin != "upload":
            repository._artifacts._rows[key] = artifact.model_copy(
                update={"expires_at": min(artifact.expires_at or erased_at, erased_at)}
            )
            artifact_ids.add(artifact.id)
    for key, export in list(repository._trajectory_exports._rows.items()):
        if export.run_id in runs:
            repository._trajectory_exports._rows[key] = export.model_copy(
                update={
                    "artifact": export.artifact.model_copy(
                        update={
                            "expires_at": min(export.artifact.expires_at or erased_at, erased_at)
                        }
                    )
                }
            )
            artifact_ids.add(export.artifact.id)
    documents = {
        key
        for key, document in repository._knowledge._documents.items()
        if document.source_ref.id in artifact_ids
    }
    repository._knowledge._documents = {
        key: value
        for key, value in repository._knowledge._documents.items()
        if key not in documents
    }
    repository._knowledge._chunks = {
        key: value
        for key, value in repository._knowledge._chunks.items()
        if value.document_row_id not in documents
    }
    return PeopleCopyCleanup(
        active_run_ids=active,
        pending_run_ids=sorted(runs) if active or len(artifact_ids) > 256 else [],
        artifact_ids=sorted(artifact_ids)[:256],
        counts={
            "active_runs": len(active),
            "events": count,
            "invocations": len(changed_invocations),
            "checkpoints": checkpoints,
            "pending_artifacts": len(artifact_ids),
        },
    )
