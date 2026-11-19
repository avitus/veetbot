"""Atomic call erasure. Dependency sets live in transaction-local PostgreSQL tables."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, cast

from sqlalchemy import literal_column, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence import call_erasure
from agent_core.adapters.persistence.email_erasure import erase_trace
from agent_core.adapters.persistence.sqlalchemy_models import (
    EventRow,
    RecallTraceRow,
    SessionHistoryItemRow,
    ToolInvocationRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import TERMINAL_RUN_STATUSES

_BATCH = 64


async def _rows(
    session: AsyncSession,
    model: type[Any],
    sql: str,
    *flags: str,
    params: dict[str, Any] | None = None,
) -> AsyncIterator[Any]:
    statement = (
        select(model, *(literal_column(flag) for flag in flags))
        .from_statement(text(sql))
        .execution_options(yield_per=_BATCH)
    )
    cursor = await session.stream(statement, params)
    try:
        async for page in cursor.partitions(_BATCH):
            for row in page:
                yield row
            # Dirty ORM objects are strongly retained until flushed.
            await session.flush()
    finally:
        await cursor.close()


async def erase_postgres_call(
    session: AsyncSession, principal: Principal, call_id: str, erased_at: datetime
) -> dict[str, int]:
    owner = {"tenant": principal.tenant_id, "owner": principal.principal_id}
    counts = dict.fromkeys(
        ("events", "invocations", "checkpoints", "episodes", "pending_artifacts"), 0
    )
    await _prepare_work_tables(session)
    await _lock_sessions(session, owner)
    await _find_call_reads(session, call_id)
    if not await session.scalar(text("SELECT EXISTS (SELECT FROM pg_temp.call_sources)")):
        return counts
    await _find_dependents(session, owner)
    counts["events"] = await _redact_events(session, call_id)
    counts["invocations"] = await _redact_invocations(session, call_id)
    await _redact_history(session, call_id)
    await _purge_derived_records(session, owner, erased_at, counts)
    return counts


async def _prepare_work_tables(session: AsyncSession) -> None:
    # PostgreSQL closes server cursors and drops their tables together at transaction end.
    # A repository caller may erase two sources in one UoW. Use fresh contents each time.
    reuse = await session.scalar(text("SELECT to_regclass('pg_temp.call_owned') IS NOT NULL"))
    for name, columns in (
        ("call_owned", "id uuid PRIMARY KEY"),
        (
            "call_sources",
            "session_id uuid, sequence bigint, runtime boolean, PRIMARY KEY(session_id,sequence)",
        ),
        (
            "call_sequences",
            "session_id uuid, sequence bigint, run_id uuid, PRIMARY KEY(session_id,sequence)",
        ),
        ("call_runs", "id uuid PRIMARY KEY"),
        ("call_sessions", "id uuid PRIMARY KEY"),
        ("call_belief_events", "id bigint PRIMARY KEY, belief_id text"),
        ("call_beliefs", "belief_id text PRIMARY KEY"),
    ):
        if reuse:
            await session.execute(text(f"DELETE FROM pg_temp.{name}"))
        else:
            await session.execute(text(f"CREATE TEMP TABLE {name} ({columns}) ON COMMIT DROP"))


async def _lock_sessions(session: AsyncSession, owner: dict[str, str]) -> None:
    # Preserve the FK/append fence. Lock in stable order, transfer no session metadata,
    # and release all locks together with the tombstone in the caller's transaction.
    await session.execute(
        text("""
        INSERT INTO pg_temp.call_owned
        SELECT id FROM sessions WHERE tenant_id=:tenant AND principal_id=:owner
        ORDER BY id FOR UPDATE
    """),
        owner,
    )
    await session.execute(text("ANALYZE pg_temp.call_owned"))
    active = await session.scalar(
        text("""
        SELECT r.id FROM runs r JOIN pg_temp.call_owned o ON o.id=r.session_id
        WHERE r.status != ALL(CAST(:terminal AS text[])) ORDER BY r.id LIMIT 1
    """),
        {"terminal": [s.value for s in TERMINAL_RUN_STATUSES]},
    )
    if active is not None:
        raise ConflictError(
            "associated runs must settle before correspondence source erasure",
            reason="active_run_exists",
            details={"run_id": str(active)},
        )


async def _find_call_reads(session: AsyncSession, call_id: str) -> None:
    # Inspect only typed read-tool events. The selector handles nested JSON strings,
    # including escaped call identifiers, exactly as the source redactor does.
    names = sorted(call_erasure.READ_TOOLS)
    candidates = await session.stream(
        text("""
        SELECT e.session_id, e.sequence, e.actor_type, e.payload FROM events e
        JOIN pg_temp.call_owned o ON o.id=e.session_id
        WHERE e.payload->>'name' = ANY(CAST(:names AS text[]))
    """).execution_options(yield_per=_BATCH),
        {"names": names},
    )
    try:
        async for batch in candidates.partitions(_BATCH):
            matches = [
                {"sid": sid, "seq": seq, "runtime": actor == "runtime"}
                for sid, seq, actor, payload in batch
                if call_erasure.source_present(payload, call_id, frozenset())
            ]
            if matches:
                await session.execute(
                    text("""
                    INSERT INTO pg_temp.call_sources VALUES (:sid, :seq, :runtime)
                """),
                    matches,
                )
    finally:
        await candidates.close()

    await session.execute(
        text("""
        DELETE FROM pg_temp.call_sources s WHERE NOT EXISTS (
            SELECT FROM pg_temp.call_sources r WHERE r.session_id=s.session_id AND r.runtime)
    """)
    )


async def _find_dependents(session: AsyncSession, owner: dict[str, str]) -> None:
    # Keep exact non-owner sequences after the first read, including later turns
    # with no textual identifier. This relation can be arbitrarily large.
    await session.execute(
        text("""
        INSERT INTO pg_temp.call_sequences
        SELECT e.session_id, e.sequence,
            CASE WHEN e.actor_type <> 'principal' THEN e.run_id END AS run_id FROM events e
        JOIN (SELECT session_id, min(sequence) AS first FROM pg_temp.call_sources
              GROUP BY session_id) s USING (session_id)
        WHERE e.sequence >= s.first AND (e.actor_type <> 'principal' OR EXISTS (
            SELECT FROM pg_temp.call_sources src WHERE src.session_id=e.session_id
            AND src.sequence=e.sequence))
    """)
    )
    await session.execute(text("ANALYZE pg_temp.call_sequences"))
    await session.execute(
        text("""
        INSERT INTO pg_temp.call_runs SELECT DISTINCT r.id FROM pg_temp.call_sequences s
        JOIN runs r ON r.id=s.run_id AND r.session_id=s.session_id
    """)
    )
    await session.execute(
        text("""
        INSERT INTO pg_temp.call_belief_events
        SELECT e.id, e.payload->'belief'->>'id' AS belief_id FROM events e
        JOIN (SELECT DISTINCT session_id FROM pg_temp.call_sources) src USING (session_id)
        WHERE e.payload->'belief'->>'authority'='inferred'
          AND e.payload->'belief'->>'source_session_id'=e.session_id::text
          AND EXISTS (SELECT FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(e.payload->'belief'->'source_event_ids')='array'
            THEN e.payload->'belief'->'source_event_ids' ELSE '[]'::jsonb END) ref
            JOIN pg_temp.call_sequences s ON s.session_id=e.session_id
              AND to_jsonb(s.sequence)=ref)
    """)
    )
    await session.execute(
        text("""
        INSERT INTO pg_temp.call_beliefs
        SELECT DISTINCT belief_id FROM pg_temp.call_belief_events b WHERE belief_id IS NOT NULL
        AND NOT EXISTS (SELECT FROM memories m WHERE m.tenant_id=:tenant
          AND m.principal_id=:owner AND m.authority<>'inferred' AND m.id::text=b.belief_id)
    """),
        owner,
    )
    await session.execute(
        text(
            "INSERT INTO pg_temp.call_sessions SELECT DISTINCT session_id FROM pg_temp.call_sources"
        )
    )
    if await session.scalar(text("SELECT EXISTS (SELECT FROM pg_temp.call_beliefs)")):
        await _recall_dependencies(session, owner)


async def _redact_events(session: AsyncSession, call_id: str) -> int:
    changed = 0
    async for row, belief_event, derived in _rows(
        session,
        EventRow,
        """
        SELECT e.*, b.id IS NOT NULL AS belief_event,
            (r.id IS NOT NULL OR seq.sequence IS NOT NULL) AS derived
        FROM events e JOIN pg_temp.call_sessions cs ON cs.id=e.session_id
        LEFT JOIN pg_temp.call_belief_events b ON b.id=e.id
        LEFT JOIN pg_temp.call_runs r ON r.id=e.run_id
        LEFT JOIN pg_temp.call_sequences seq USING (session_id, sequence)
        WHERE e.actor_type<>'principal' AND (
            r.id IS NOT NULL OR seq.sequence IS NOT NULL OR b.id IS NOT NULL)
    """,
        "belief_event",
        "derived",
    ):
        payload = row.payload
        if row.actor_type == "runtime" and call_erasure.source_tool(
            payload.get("name"), "bland_read"
        ):
            payload = call_erasure.erase_result(payload, call_id, frozenset())
        if payload != row.payload:
            changed += 1
        else:
            if derived:
                payload = call_erasure.erase_derived(payload)
            if belief_event:
                payload = {
                    **payload,
                    "belief": {**payload["belief"], "statement": "[email source erased]"},
                    "source_erased": True,
                }
        if payload != row.payload:
            row.payload = payload

    return changed


async def _redact_invocations(session: AsyncSession, call_id: str) -> int:
    count = 0
    async for inv, affected in _rows(
        session,
        ToolInvocationRow,
        """
        SELECT i.*, r.id IS NOT NULL AS affected FROM tool_invocations i
        JOIN pg_temp.call_sessions s ON s.id=i.session_id
        LEFT JOIN pg_temp.call_runs r ON r.id=i.run_id
        WHERE r.id IS NOT NULL OR i.tool_name=ANY(CAST(:names AS text[]))
    """,
        "affected",
        params={"names": sorted(call_erasure.READ_TOOLS)},
    ):
        # Other runs in a trace/source session can have an invocation receipt only.
        if affected:
            for field in (
                "raw_arguments",
                "arguments",
                "result_item",
                "structured_result",
                "outcome",
            ):
                setattr(inv, field, call_erasure.erase_derived({field: getattr(inv, field)})[field])
            count += 1
        else:
            changed = False
            for field in ("result_item", "structured_result", "outcome"):
                value = getattr(inv, field)
                erased = call_erasure.erase_result(value, call_id, frozenset())
                if value != erased:
                    setattr(inv, field, erased)
                    changed = True
            count += int(changed)

    return count


async def _redact_history(session: AsyncSession, call_id: str) -> None:
    async for (history,) in _rows(
        session,
        SessionHistoryItemRow,
        """
        SELECT h.* FROM session_history_items h JOIN events e USING (session_id, sequence)
        JOIN pg_temp.call_owned o ON o.id=h.session_id
        WHERE e.actor_type<>'principal' AND (e.run_id IN (SELECT id FROM pg_temp.call_runs)
        OR (e.session_id,e.sequence) IN (SELECT session_id,sequence FROM pg_temp.call_sequences))
    """,
    ):
        history.item = call_erasure.erase_derived(
            call_erasure.erase_result(history.item, call_id, frozenset())
        )


async def _purge_derived_records(
    session: AsyncSession, owner: dict[str, str], erased_at: datetime, counts: dict[str, int]
) -> None:
    await session.execute(
        text("""
        DELETE FROM memory_revisions m WHERE tenant_id=:tenant AND principal_id=:owner
        AND EXISTS (SELECT FROM pg_temp.call_sequences s WHERE
          m.payload->>'source_session_id'=s.session_id::text
          AND m.payload->'source_event_ids' @> jsonb_build_array(s.sequence))
    """),
        owner,
    )
    for key, sql in (
        (
            "checkpoints",
            "DELETE FROM checkpoints WHERE run_id IN (SELECT id FROM pg_temp.call_runs)",
        ),
        (
            "episodes",
            """DELETE FROM integrated_episodes ep WHERE tenant_id=:tenant AND principal_id=:owner
            AND EXISTS (SELECT FROM pg_temp.call_sequences s WHERE s.session_id=ep.session_id
            AND ep.source_event_ids @> jsonb_build_array(s.sequence))""",
        ),
        (
            "pending_artifacts",
            """UPDATE artifacts SET expires_at=:erased_at
            WHERE tenant_id=:tenant AND principal_id=:owner AND origin<>'upload'
            AND run_id IN (SELECT id FROM pg_temp.call_runs)""",
        ),
    ):
        result = await session.execute(text(sql), {**owner, "erased_at": erased_at})
        counts[key] = cast(CursorResult[Any], result).rowcount
    await session.execute(
        text("UPDATE runs SET final_message=NULL WHERE id IN (SELECT id FROM pg_temp.call_runs)")
    )
    await session.execute(
        text("""
        DELETE FROM knowledge_documents WHERE source_artifact_id IN (
            SELECT id FROM artifacts WHERE tenant_id=:tenant AND principal_id=:owner
            AND origin<>'upload' AND run_id IN (SELECT id FROM pg_temp.call_runs))
    """),
        owner,
    )


async def _recall_dependencies(session: AsyncSession, owner: dict[str, str]) -> None:
    async for (row,) in _rows(
        session,
        RecallTraceRow,
        """
        SELECT t.* FROM recall_traces t JOIN pg_temp.call_owned o ON o.id=t.session_id
        WHERE t.tenant_id=:tenant AND t.principal_id=:owner
    """,
        params=owner,
    ):
        items = row.trace.get("beliefs", [])
        candidates = {str(b.get("belief_id")) for b in items if isinstance(b, dict)}
        candidates.update(
            str(i) for b in items if isinstance(b, dict) for i in b.get("support_ids", [])
        )
        for field in ("returned", "cited", "carried_in", "blocked", "dropped_for_budget"):
            candidates.update(str(item) for item in row.trace.get(field, []))
        ids = await session.scalars(
            text("""
            SELECT belief_id FROM pg_temp.call_beliefs WHERE belief_id=ANY(CAST(:ids AS text[]))
        """),
            {"ids": list(candidates)},
        )
        changed = erase_trace(row.trace, set(ids))
        if changed is None:
            continue
        row.trace = changed
        await session.execute(
            text("INSERT INTO pg_temp.call_sessions VALUES (:id) ON CONFLICT DO NOTHING"),
            {"id": row.session_id},
        )
        if row.trace.get("run_id"):
            await session.execute(
                text("""
                INSERT INTO pg_temp.call_runs SELECT r.id FROM runs r JOIN pg_temp.call_owned o
                ON o.id=r.session_id WHERE r.id=:id ON CONFLICT DO NOTHING
            """),
                {"id": row.trace["run_id"]},
            )
    await session.execute(
        text("""
        INSERT INTO pg_temp.call_runs SELECT DISTINCT r.id FROM runs r
        JOIN pg_temp.call_owned o ON o.id=r.session_id JOIN checkpoints c ON c.run_id=r.id
        WHERE EXISTS (SELECT FROM jsonb_path_query(
            c.state, 'strict $.** ? (@.type() == "string")') v
            JOIN pg_temp.call_beliefs b ON strpos(v #>> '{}', b.belief_id)>0)
        ON CONFLICT DO NOTHING
    """)
    )
    await session.execute(
        text("""
        INSERT INTO pg_temp.call_sessions SELECT DISTINCT session_id FROM runs
        WHERE id IN (SELECT id FROM pg_temp.call_runs) ON CONFLICT DO NOTHING
    """)
    )
