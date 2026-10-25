"""Read-only PostgreSQL aggregates for the recall health report."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_core.domain.agents import Principal
from agent_core.observability.memory_health import (
    ClassShare,
    LifecycleCount,
    MemoryHealthReport,
)

_EVENTS = (
    "events e join sessions s on s.id = e.session_id "
    "where s.tenant_id = :tenant and s.principal_id = :principal "
    "and e.created_at >= :since and e.created_at <= :until"
)
_USAGE = f"""
select count(distinct e.run_id) filter (where e.event_type = 'memory.recalled') as recalled_runs,
       count(distinct e.run_id) filter (where e.event_type = 'memory.delivered') as delivered_runs,
       count(distinct e.run_id) filter (where e.event_type = 'memory.cited'
         and jsonb_array_length(e.payload->'cited') > 0) as cited_runs,
       coalesce(sum(jsonb_array_length(e.payload->'cited'))
         filter (where e.event_type = 'memory.cited'), 0) as cited_beliefs,
       coalesce(sum(jsonb_array_length(e.payload->'uncited'))
         filter (where e.event_type = 'memory.cited'), 0) as charged_beliefs,
       coalesce(sum((e.payload->>'ambiguous')::int)
         filter (where e.event_type = 'memory.cited'), 0) as ambiguous
from {_EVENTS}
"""
_TRACES = """
with scoped as (
  select t.trace,
         (select coalesce(sum(value::numeric), 0)
            from jsonb_each_text(coalesce(t.trace->'arm_latencies_ms', '{}'::jsonb))) as latency
  from recall_traces t
  where t.tenant_id = :tenant and t.principal_id = :principal
    and t.created_at >= :since and t.created_at <= :until and not t.erasure_pending
)
select count(*) as traces,
       count(*) filter (where trace->'arm_latencies_ms' ? 'lexical') as with_lexical,
       count(*) filter (where trace->'arm_latencies_ms' ? 'structured') as with_structured,
       count(*) filter (where trace->'arm_latencies_ms' ? 'semantic') as with_semantic,
       percentile_cont(0.5) within group (order by latency) as p50,
       percentile_cont(0.95) within group (order by latency) as p95,
       count(*) filter (where latency > 1000) as slow
from scoped
"""


def _class_share(column: str) -> str:
    return f"""
with ids as (
  select (value)::uuid as belief_id
  from recall_traces t, jsonb_array_elements_text(coalesce(t.trace->'{column}', '[]'::jsonb))
  where t.tenant_id = :tenant and t.principal_id = :principal
    and t.created_at >= :since and t.created_at <= :until and not t.erasure_pending
)
select count(*) as total,
       count(*) filter (where m.flagged_for_review) as flagged,
       count(*) filter (where m.status = 'provisional') as provisional,
       count(*) filter (where m.consolidation_policy_version like 'owner-source-learning@%')
         as source_learned,
       count(*) filter (where m.authority in ('user', 'affirmed')) as owner_authority
from ids left join memories m on m.id = ids.belief_id
"""


_STORE = """
select count(*) as live,
       count(*) filter (where status = 'active') as active,
       count(*) filter (where status = 'provisional') as provisional,
       count(*) filter (where flagged_for_review) as flagged,
       count(*) filter (where authority in ('user', 'affirmed')) as owner_authority,
       count(*) filter (where last_used_at is null) as never_used,
       count(*) filter (where usage_accounting_version is not null) as counted
from memories
where tenant_id = :tenant and principal_id = :principal
  and status in ('active', 'provisional') and not erasure_pending
"""
_LIFECYCLE = f"""
select e.event_type, coalesce(e.payload->'belief'->>'authority', 'unknown') as authority,
       count(*) as count,
       min((e.payload->'belief'->>'confidence')::float) as minimum_confidence
from {_EVENTS} and e.event_type in ('memory.decayed', 'memory.retired', 'memory.superseded')
group by 1, 2 order by 1, 2
"""
_INDEX = """
select count(distinct m.id) as indexed
from memories m join memory_vectors v
  on v.tenant_id = m.tenant_id and v.principal_id = m.principal_id and v.belief_id = m.id
  and v.content_revision = m.content_revision and v.creation_sequence = m.creation_sequence
where m.tenant_id = :tenant and m.principal_id = :principal
  and m.status in ('active', 'provisional') and not m.erasure_pending
"""
_JOB = """
select (payload->>'generation')::int as generation, payload->>'state' as state,
       (payload->>'failed_pages')::int as failed_pages
from semantic_index_jobs
where tenant_id = :tenant and principal_id = :principal
order by (payload->>'generation')::int desc nulls last limit 1
"""


async def read_memory_health_report(
    session: AsyncSession, since: datetime, until: datetime, *, principal: Principal
) -> MemoryHealthReport:
    """Read aggregates inside the caller's transaction; never select statements."""

    if (
        since.tzinfo is None
        or until.tzinfo is None
        or not timedelta(0) < until - since <= timedelta(days=90)
    ):
        raise ValueError("Recall health windows must be aware, positive and at most ninety days.")
    parameters = {
        "tenant": principal.tenant_id,
        "principal": principal.principal_id,
        "since": since,
        "until": until,
    }

    async def rows(statement: str) -> list[Any]:
        return list((await session.execute(text(statement), parameters)).all())

    [usage] = await rows(_USAGE)
    [traces] = await rows(_TRACES)
    [returned] = await rows(_class_share("returned"))
    [dropped] = await rows(_class_share("dropped_for_budget"))
    [store] = await rows(_STORE)
    lifecycle = await rows(_LIFECYCLE)
    [index] = await rows(_INDEX)
    jobs = await rows(_JOB)
    job = jobs[0] if jobs else None
    return MemoryHealthReport(
        since=since,
        until=until,
        recalled_runs=usage.recalled_runs,
        delivered_runs=usage.delivered_runs,
        cited_runs=usage.cited_runs,
        cited_beliefs=int(usage.cited_beliefs),
        charged_beliefs=int(usage.charged_beliefs),
        ambiguous_citations=int(usage.ambiguous),
        traces=traces.traces,
        traces_with_lexical=traces.with_lexical,
        traces_with_structured=traces.with_structured,
        traces_with_semantic=traces.with_semantic,
        p50_recall_latency_ms=None if traces.p50 is None else max(0.0, float(traces.p50)),
        p95_recall_latency_ms=None if traces.p95 is None else max(0.0, float(traces.p95)),
        slow_traces=traces.slow,
        returned=_share(returned),
        dropped_for_budget=_share(dropped),
        live_beliefs=store.live,
        active_beliefs=store.active,
        provisional_beliefs=store.provisional,
        flagged_beliefs=store.flagged,
        owner_authority_beliefs=store.owner_authority,
        never_used_beliefs=store.never_used,
        counted_beliefs=store.counted,
        lifecycle=[
            LifecycleCount(
                event_type=row.event_type,
                authority=row.authority,
                count=row.count,
                minimum_confidence=(
                    None if row.minimum_confidence is None else float(row.minimum_confidence)
                ),
            )
            for row in lifecycle
        ],
        indexed_live_beliefs=index.indexed,
        index_generation=None if job is None else job.generation,
        index_state=None if job is None else job.state,
        index_failed_pages=None if job is None else job.failed_pages,
    )


def _share(row: Any) -> ClassShare:
    return ClassShare(
        total=row.total,
        flagged=row.flagged,
        provisional=row.provisional,
        source_learned=row.source_learned,
        owner_authority=row.owner_authority,
    )


async def memory_health_report(
    sessions: async_sessionmaker[AsyncSession],
    since: datetime,
    until: datetime,
    *,
    principal: Principal,
) -> MemoryHealthReport:
    """One consistent, read-only snapshot, with ten-second statement bounds."""

    async with sessions() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '10s'"))
        return await read_memory_health_report(session, since, until, principal=principal)
