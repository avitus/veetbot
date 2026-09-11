"""Selective source-content erasure; immutable audit identity is retained."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import (
    ArtifactRow,
    CheckpointRow,
    EventRow,
    IntegratedEpisodeRow,
    KnowledgeDocumentRow,
    MemoryRow,
    RecallTraceRow,
    RunRow,
    SessionHistoryItemRow,
    SessionRow,
    ToolInvocationRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import TERMINAL_RUN_STATUSES

_ERASED = "[email source erased]"
_IDENTITY_FIELDS = frozenset({"id", "message_id", "thread_id", "history_id", "internal_date"})
_UNFINISHED_INVOCATIONS = ("PROPOSED", "AUTHORIZED", "WAITING_FOR_APPROVAL", "RUNNING")


def email_read_capability(scopes: Any, server: str | None) -> bool:
    """Fail closed while an existing run can still produce a first email result."""
    return server is not None or any(
        scope == "email.read"
        or (
            isinstance(scope, str)
            and re.fullmatch(r"mcp\.gmail(?:_[a-z][a-z0-9_]*)?_read\..+", scope)
        )
        for scope in scopes
    )


def erase_result(
    value: Any,
    thread_id: str,
    message_ids: frozenset[str],
    inherited_thread: str | None = None,
    *,
    account_id: str | None = None,
) -> Any:
    """Remove only a selected normalized message, including JSON-wrapped tool content."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        if not isinstance(decoded, (dict, list)):
            return value
        erased = erase_result(
            decoded, thread_id, message_ids, inherited_thread, account_id=account_id
        )
        return json.dumps(erased, ensure_ascii=False) if erased != decoded else value
    if isinstance(value, list):
        return [
            erase_result(item, thread_id, message_ids, inherited_thread, account_id=account_id)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    binding = value.get("source")
    if isinstance(binding, dict) and "account_id" in binding:
        if (
            binding.get("account_id") != account_id
            or binding.get("provider_thread_id") != thread_id
        ):
            return value
        inherited_thread = thread_id
        if set(binding.get("message_ids", [])).intersection(message_ids):
            value = {
                key: item
                for key, item in value.items()
                if key not in {"summary", "reason", "subject"}
            }
            thread = value.get("thread")
            if isinstance(thread, dict):
                value["thread"] = {
                    key: item
                    for key, item in thread.items()
                    if key not in {"summary", "reason", "subject"}
                }
    source_thread = value.get("thread_id", inherited_thread)
    message_id = value.get("id", value.get("message_id"))
    target = (
        (source_thread == thread_id or (source_thread is None and "message_id" in value))
        and isinstance(message_id, str)
        and message_id in message_ids
        and any(key in value for key in ("body", "from", "snippet"))
    )
    preview = source_thread == thread_id and message_id is None and "snippet" in value
    if target or preview:
        return {
            **{key: item for key, item in value.items() if key in _IDENTITY_FIELDS},
            "source_erased": True,
            "body_complete": False,
            "headers_complete": False,
        }
    if isinstance(message_id, str) and any(key in value for key in ("body", "from", "snippet")):
        return value
    return {
        key: erase_result(item, thread_id, message_ids, source_thread, account_id=account_id)
        for key, item in value.items()
    }


def source_present(
    value: Any,
    thread_id: str,
    message_ids: frozenset[str],
    inherited: str | None = None,
    *,
    account_id: str | None = None,
) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    if isinstance(value, list):
        return any(
            source_present(item, thread_id, message_ids, inherited, account_id=account_id)
            for item in value
        )
    if not isinstance(value, dict):
        return False
    binding = value.get("source")
    if isinstance(binding, dict) and "account_id" in binding:
        return (
            binding.get("account_id") == account_id
            and binding.get("provider_thread_id") == thread_id
            and bool(set(binding.get("message_ids", [])).intersection(message_ids))
        )
    current = value.get("thread_id", inherited)
    message_id = value.get("id", value.get("message_id"))
    if (
        (current == thread_id or (current is None and "message_id" in value))
        and isinstance(message_id, str)
        and message_id in message_ids
    ):
        return True
    if isinstance(message_id, str) and any(key in value for key in ("body", "from", "snippet")):
        return False
    return any(
        source_present(item, thread_id, message_ids, current, account_id=account_id)
        for item in value.values()
    )


def erase_belief(payload: dict[str, Any], session_id: UUID, sequences: set[int]) -> dict[str, Any]:
    belief = payload.get("belief")
    if (
        isinstance(belief, dict)
        and belief.get("authority") == "inferred"
        and belief.get("source_session_id") == str(session_id)
        and set(belief.get("source_event_ids", [])).intersection(sequences)
    ):
        return {**payload, "belief": {**belief, "statement": _ERASED}, "source_erased": True}
    return payload


def source_belief_ids(
    events: list[Any], sequences: dict[UUID, set[int]], protected: set[str]
) -> set[str]:
    identifiers = set()
    for event in events:
        belief = event.payload.get("belief")
        if (
            isinstance(belief, dict)
            and belief.get("authority") == "inferred"
            and belief.get("source_session_id") == str(event.session_id)
            and set(belief.get("source_event_ids", [])).intersection(
                sequences.get(event.session_id, set())
            )
            and isinstance(belief.get("id"), str)
            and belief["id"] not in protected
        ):
            identifiers.add(belief["id"])
    return identifiers


def erase_trace(value: dict[str, Any], belief_ids: set[str]) -> dict[str, Any] | None:
    beliefs = value.get("beliefs", [])
    if not any(
        str(item.get("belief_id")) in belief_ids for item in beliefs if isinstance(item, dict)
    ):
        return None
    result = {
        **value,
        "beliefs": [item for item in beliefs if str(item.get("belief_id")) not in belief_ids],
        "rendered": "",
        "rendered_sha256": hashlib.sha256(b"").hexdigest(),
    }
    for field in ("returned", "cited", "carried_in", "blocked", "dropped_for_budget"):
        result[field] = [item for item in result.get(field, []) if str(item) not in belief_ids]
    return result


def contains_belief(value: Any, belief_ids: set[str]) -> bool:
    if isinstance(value, str):
        return any(identifier in value for identifier in belief_ids)
    if isinstance(value, list):
        return any(contains_belief(item, belief_ids) for item in value)
    if isinstance(value, dict):
        return any(contains_belief(item, belief_ids) for item in value.values())
    return False


def read_server(metadata: dict[str, Any], account_id: str) -> str | None:
    bindings = metadata.get("email_account_servers")
    binding = bindings.get(account_id) if isinstance(bindings, dict) else None
    return binding.get("read") if isinstance(binding, dict) else None


def source_tool(name: object, server: str | None) -> bool:
    if name == "email.context":
        return True
    return (
        server is not None
        and isinstance(name, str)
        and name
        in {
            f"mcp.{server}.{operation}"
            for operation in ("get_thread", "get_thread_page", "get_message_body", "search_threads")
        }
    )


def derived_payload(payload: dict[str, Any], event_type: str) -> dict[str, Any]:
    copied = deepcopy(payload)
    if event_type == "assistant.message.completed":
        message = copied.get("message")
        if isinstance(message, dict):
            message["content"] = [{"type": "text", "text": _ERASED}]
        if "content" in copied:
            copied["content"] = _ERASED
    elif event_type == "run.completed" and "final_message" in copied:
        copied["final_message"] = _ERASED
    return copied


async def erase_postgres_source(
    session: AsyncSession,
    principal: Principal,
    account_id: str,
    thread_id: str,
    message_ids: frozenset[str],
    erased_at: datetime,
) -> dict[str, int]:
    # The application appends the separate immutable erasure audit event.
    owned = list(
        (
            await session.scalars(
                select(SessionRow)
                .where(
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
                .with_for_update()
            )
        ).all()
    )
    servers = {row.id: read_server(row.metadata_json, account_id) for row in owned}
    event_rows = list(
        (
            await session.scalars(
                select(EventRow)
                .where(
                    EventRow.session_id.in_(servers),
                )
                .order_by(EventRow.id)
            )
        ).all()
    )
    changes: dict[int, dict[str, Any]] = {}
    affected_sessions: set[UUID] = set()
    affected_runs: set[UUID] = set()
    for row in event_rows:
        if row.actor_type != "runtime" or not source_tool(
            row.payload.get("name"), servers[row.session_id]
        ):
            continue
        erased = erase_result(row.payload, thread_id, message_ids, account_id=account_id)
        if erased != row.payload:
            changes[row.id] = erased
        if source_present(row.payload, thread_id, message_ids, account_id=account_id):
            affected_sessions.add(row.session_id)
            if row.run_id is not None:
                affected_runs.add(row.run_id)
    source_sequences = {
        sid: {
            row.sequence
            for row in event_rows
            if row.session_id == sid
            and source_tool(row.payload.get("name"), servers.get(sid))
            and source_present(row.payload, thread_id, message_ids, account_id=account_id)
        }
        for sid in affected_sessions
    }
    protected = {
        str(identifier)
        for identifier in (
            await session.scalars(
                select(MemoryRow.id).where(
                    MemoryRow.tenant_id == principal.tenant_id,
                    MemoryRow.principal_id == principal.principal_id,
                    MemoryRow.authority != "inferred",
                )
            )
        ).all()
    }
    belief_ids = source_belief_ids(event_rows, source_sequences, protected)
    trace_changes: list[tuple[RecallTraceRow, dict[str, Any]]] = []
    if belief_ids:
        for trace_row in (
            await session.scalars(
                select(RecallTraceRow).where(
                    RecallTraceRow.tenant_id == principal.tenant_id,
                    RecallTraceRow.principal_id == principal.principal_id,
                )
            )
        ).all():
            erased_trace = erase_trace(trace_row.trace, belief_ids)
            if erased_trace is not None:
                trace_changes.append((trace_row, erased_trace))
                affected_sessions.add(trace_row.session_id)
                if trace_row.trace.get("run_id"):
                    affected_runs.add(UUID(trace_row.trace["run_id"]))
        owned_runs = list(
            (
                await session.scalars(
                    select(RunRow).where(
                        RunRow.session_id.in_(servers),
                    )
                )
            ).all()
        )
        run_sessions = {run.id: run.session_id for run in owned_runs}
        for snapshot in (
            await session.scalars(
                select(CheckpointRow).where(
                    CheckpointRow.run_id.in_(run_sessions),
                )
            )
        ).all():
            if contains_belief(snapshot.state, belief_ids):
                affected_runs.add(snapshot.run_id)
                affected_sessions.add(run_sessions[snapshot.run_id])
        known_ids = {event.id for event in event_rows}
        for dependent in (
            await session.scalars(
                select(EventRow).where(
                    EventRow.session_id.in_(affected_sessions),
                )
            )
        ).all():
            if dependent.id not in known_ids:
                event_rows.append(dependent)
    pending_context = set(
        (
            await session.scalars(
                select(ToolInvocationRow.run_id).where(
                    ToolInvocationRow.session_id.in_(servers),
                    ToolInvocationRow.tool_name == "email.context",
                    ToolInvocationRow.status.in_(_UNFINISHED_INVOCATIONS),
                )
            )
        ).all()
    )
    current_runs = (
        await session.scalars(
            select(RunRow)
            .where(
                RunRow.session_id.in_(servers),
                RunRow.status.not_in([item.value for item in TERMINAL_RUN_STATUSES]),
            )
            .order_by(RunRow.id)
        )
    ).all()
    active = next(
        (
            run.id
            for run in current_runs
            if (
                run.session_id in affected_sessions
                or run.id in pending_context
                or email_read_capability(run.principal_scopes, servers.get(run.session_id))
            )
        ),
        None,
    )
    if active is not None:
        raise ConflictError(
            "associated runs must settle before email source erasure",
            reason="active_run_exists",
            details={"run_id": str(active)},
        )
    counts = {
        "events": len(changes),
        "invocations": 0,
        "checkpoints": 0,
        "episodes": 0,
        "pending_artifacts": 0,
    }
    for trace_row, erased_trace in trace_changes:
        trace_row.trace = erased_trace
    for row in event_rows:
        payload = changes.get(row.id)
        if payload is None and row.actor_type != "principal":
            payload = erase_belief(
                row.payload, row.session_id, source_sequences.get(row.session_id, set())
            )
        if (
            (payload is None or payload == row.payload)
            and row.run_id in affected_runs
            and row.actor_type != "principal"
        ):
            payload = derived_payload(row.payload, row.event_type)
        if payload is not None and payload != row.payload:
            row.payload = payload
    invocations = list(
        (
            await session.scalars(
                select(ToolInvocationRow).where(
                    ToolInvocationRow.session_id.in_(affected_sessions),
                )
            )
        ).all()
    )
    for invocation in invocations:
        if not source_tool(invocation.tool_name, servers.get(invocation.session_id)):
            continue
        changed = False
        for field in ("result_item", "structured_result", "outcome"):
            value = getattr(invocation, field)
            erased = erase_result(value, thread_id, message_ids, account_id=account_id)
            if value != erased:
                setattr(invocation, field, erased)
                changed = True
        counts["invocations"] += int(changed)
    checkpoint_ids = list(
        (
            await session.scalars(
                select(CheckpointRow.id).where(CheckpointRow.run_id.in_(affected_runs))
            )
        ).all()
    )
    counts["checkpoints"] = len(checkpoint_ids)
    await session.execute(delete(CheckpointRow).where(CheckpointRow.id.in_(checkpoint_ids)))
    await session.execute(
        update(RunRow).where(RunRow.id.in_(affected_runs)).values(final_message=None)
    )
    # These are rebuildable views; delete only source-derived rows and regenerate
    # from the retained, now-redacted authoritative event stream.
    for sid in affected_sessions:
        sequences = [
            row.sequence for row in event_rows if row.session_id == sid and row.id in changes
        ]
        history_rows = list(
            (
                await session.scalars(
                    select(SessionHistoryItemRow).where(
                        SessionHistoryItemRow.session_id == sid,
                    )
                )
            ).all()
        )
        by_sequence = {event.sequence: event for event in event_rows if event.session_id == sid}
        for history in history_rows:
            source_event = by_sequence.get(history.sequence)
            if source_event is None or source_event.actor_type == "principal":
                continue
            if history.sequence in source_sequences.get(sid, set()):
                history.item = erase_result(
                    history.item, thread_id, message_ids, account_id=account_id
                )
            elif (
                source_event.run_id in affected_runs
                and source_event.event_type == "assistant.message.completed"
            ):
                history.item = {**history.item, "content": [{"type": "text", "text": _ERASED}]}
        episodes = list(
            (
                await session.scalars(
                    select(IntegratedEpisodeRow).where(
                        IntegratedEpisodeRow.session_id == sid,
                    )
                )
            ).all()
        )
        for episode in episodes:
            if set(episode.source_event_ids).intersection(sequences):
                await session.delete(episode)
                counts["episodes"] += 1
    artifacts = list(
        (
            await session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.run_id.in_(affected_runs),
                    ArtifactRow.origin != "upload",
                )
            )
        ).all()
    )
    for artifact in artifacts:
        artifact.expires_at = erased_at
    if artifacts:
        await session.execute(
            delete(KnowledgeDocumentRow).where(
                KnowledgeDocumentRow.source_artifact_id.in_([artifact.id for artifact in artifacts])
            )
        )
    counts["pending_artifacts"] = len(artifacts)
    return counts


def erase_memory_source_locked(
    repository: Any,
    principal: Principal,
    account_id: str,
    thread_id: str,
    message_ids: frozenset[str],
    erased_at: datetime,
) -> dict[str, int]:
    servers = {
        sid: read_server(row.metadata, account_id)
        for sid, row in repository._sessions._sessions.items()
        if row.tenant_id == principal.tenant_id and row.principal_id == principal.principal_id
    }
    changes = {}
    affected_sessions: set[UUID] = set()
    affected_runs: set[UUID] = set()
    for sid, server in servers.items():
        for event in repository._events._events.get(sid, []):
            if event.actor_type != "runtime" or not source_tool(event.payload.get("name"), server):
                continue
            erased = erase_result(event.payload, thread_id, message_ids, account_id=account_id)
            if erased != event.payload:
                changes[event.id] = erased
            if source_present(event.payload, thread_id, message_ids, account_id=account_id):
                affected_sessions.add(sid)
                if event.run_id is not None:
                    affected_runs.add(event.run_id)
    sequences = {
        sid: {
            event.sequence
            for event in repository._events._events[sid]
            if source_tool(event.payload.get("name"), servers.get(sid))
            and source_present(event.payload, thread_id, message_ids, account_id=account_id)
        }
        for sid in affected_sessions
    }
    protected = {
        str(memory.id)
        for memory in repository._memories._records.values()
        if memory.tenant_id == principal.tenant_id
        and memory.principal_id == principal.principal_id
        and memory.authority.value != "inferred"
    }
    source_events = [
        event for sid in affected_sessions for event in repository._events._events.get(sid, [])
    ]
    belief_ids = source_belief_ids(source_events, sequences, protected)
    trace_changes = {}
    for key, trace in repository._traces._traces.items():
        if trace.tenant_id != principal.tenant_id or trace.principal_id != principal.principal_id:
            continue
        erased_trace = erase_trace(trace.model_dump(mode="json"), belief_ids)
        if erased_trace is not None:
            trace_changes[key] = type(trace).model_validate(erased_trace)
            affected_sessions.add(trace.session_id)
            if trace.run_id is not None:
                affected_runs.add(trace.run_id)
    for run_id, snapshots in repository._checkpoints._checkpoints.items():
        run = repository._runs._runs.get(run_id)
        if run is None or run.session_id not in servers:
            continue
        if any(
            contains_belief(snapshot.model_dump(mode="json"), belief_ids)
            for snapshot, _full in snapshots
        ):
            affected_runs.add(run_id)
            affected_sessions.add(run.session_id)
    pending_context = {
        invocation.run_id
        for invocation in repository._invocations._invocations.values()
        if invocation.session_id in servers
        and invocation.tool_name == "email.context"
        and invocation.status.value in _UNFINISHED_INVOCATIONS
    }
    active = [
        run
        for run in repository._runs._runs.values()
        if run.session_id in servers
        and run.status not in TERMINAL_RUN_STATUSES
        and (
            run.session_id in affected_sessions
            or run.id in pending_context
            or email_read_capability(run.principal_scopes, servers.get(run.session_id))
        )
    ]
    if active:
        raise ConflictError(
            "associated runs must settle before email source erasure",
            reason="active_run_exists",
            details={"run_id": str(active[0].id)},
        )
    counts = {
        "events": len(changes),
        "invocations": 0,
        "checkpoints": 0,
        "episodes": 0,
        "pending_artifacts": 0,
    }
    repository._traces._traces.update(trace_changes)
    for sid in affected_sessions:
        updated = []
        for event in repository._events._events.get(sid, []):
            payload = changes.get(event.id)
            if event.id in changes:
                sequences.setdefault(sid, set()).add(event.sequence)
            if payload is None and event.actor_type != "principal":
                payload = erase_belief(event.payload, sid, sequences.get(sid, set()))
            if (
                (payload is None or payload == event.payload)
                and event.run_id in affected_runs
                and event.actor_type != "principal"
            ):
                payload = derived_payload(event.payload, event.event_type)
            replacement = (
                event.model_copy(update={"payload": payload}, deep=True)
                if payload is not None
                else event
            )
            updated.append(replacement)
            if event.derivation_key:
                repository._events._derived[event.derivation_key] = replacement
        repository._events._events[sid] = updated
    for key, invocation in list(repository._invocations._invocations.items()):
        if invocation.session_id not in servers or not source_tool(
            invocation.tool_name, servers.get(invocation.session_id)
        ):
            continue
        value = invocation.model_dump(mode="json")
        erased = dict(value)
        for field in ("result_item", "structured_result", "outcome"):
            erased[field] = erase_result(
                value.get(field), thread_id, message_ids, account_id=account_id
            )
        if erased != value:
            repository._invocations._invocations[key] = type(invocation).model_validate(erased)
            counts["invocations"] += 1
    for rid in affected_runs:
        checkpoints = repository._checkpoints._checkpoints.pop(rid, [])
        counts["checkpoints"] += len(checkpoints)
        if rid in repository._runs._runs:
            repository._runs._runs[rid] = repository._runs._runs[rid].model_copy(
                update={"final_message": None}
            )
    for key, episode in list(repository._episodes._records.items()):
        if set(episode.source_event_ids).intersection(sequences.get(episode.session_id, set())):
            del repository._episodes._records[key]
            repository._episodes._by_derivation = {
                k: v for k, v in repository._episodes._by_derivation.items() if v != key
            }
            counts["episodes"] += 1
    expired_artifacts = set()
    for key, artifact in list(repository._artifacts._rows.items()):
        if artifact.run_id in affected_runs and artifact.origin != "upload":
            repository._artifacts._rows[key] = artifact.model_copy(update={"expires_at": erased_at})
            expired_artifacts.add(artifact.id)
    for key, export in list(repository._trajectory_exports._rows.items()):
        if export.run_id in affected_runs:
            repository._trajectory_exports._rows[key] = export.model_copy(
                update={"artifact": export.artifact.model_copy(update={"expires_at": erased_at})}
            )
            expired_artifacts.add(export.artifact.id)
    documents = {
        key
        for key, document in repository._knowledge._documents.items()
        if document.source_ref.id in expired_artifacts
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
    counts["pending_artifacts"] = len(expired_artifacts)
    return counts
