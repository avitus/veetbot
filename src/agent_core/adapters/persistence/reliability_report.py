"""Bounded, principal-scoped reliability aggregates without selecting private content."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_core.domain.agents import Principal
from agent_core.domain.runs import FailureReason
from agent_core.observability.reliability import FailureGroup, ReliabilityReport

_SCOPE = "s.tenant_id = :tenant and s.principal_id = :principal"
_WINDOW = "r.updated_at >= :since and r.updated_at <= :until"
_RUNS = f"runs r join sessions s on s.id = r.session_id where {_SCOPE}"

_TOTALS = f"""
select count(*) filter (where r.status='COMPLETED') as completed,
       count(*) filter (where r.status='FAILED') as failed,
       count(*) filter (where r.status='CANCELLED') as cancelled,
       count(*) filter (where r.status='FAILED'
         and r.failure->>'reason'='internal_error') as internal_errors,
       percentile_cont(0.95) within group (order by extract(epoch from r.updated_at-r.created_at))
         filter (where r.status='COMPLETED') as p95
from {_RUNS} and {_WINDOW}
"""
_QUEUE = f"""
select count(*) filter (where r.status='QUEUED') as queued,
       count(*) filter (where r.status='RUNNING') as running,
       count(*) filter (where r.status in ('WAITING_FOR_APPROVAL','WAITING_FOR_USER')) as waiting,
       max(extract(epoch from :until-greatest(r.created_at, r.scheduled_for)))
         filter (where r.status='QUEUED'
           and (r.scheduled_for is null or r.scheduled_for <= :until)) as oldest,
       count(*) filter (where r.status='RUNNING' and r.lease_expires_at <= :until) as stale
from {_RUNS} and r.created_at <= :until
  and r.status in ('QUEUED','RUNNING','WAITING_FOR_APPROVAL','WAITING_FOR_USER')
"""
_FAILURES = f"""
select r.failure->>'reason' as reason, r.failure->>'error_class' as error_class,
       r.failure->'details'->>'error_fingerprint' as fingerprint,
       r.failure->'details'->>'release_id' as release_id, count(*) as count
from {_RUNS} and {_WINDOW} and r.status='FAILED'
group by 1,2,3,4 order by count(*) desc,1,2,3,4 limit 51
"""
_TOOLS = f"""
select count(*) as failures, count(*) filter (where r.status='COMPLETED') as nonfatal
from tool_invocations t join runs r on r.id=t.run_id join sessions s on s.id=r.session_id
where {_SCOPE} and t.updated_at >= :since and t.updated_at <= :until
  and t.status in ('FAILED','UNCERTAIN')
"""
_MODELS = f"""
select count(*) as failures, count(*) filter (where r.status='COMPLETED') as nonfatal
from model_calls m join runs r on r.id=m.run_id join sessions s on s.id=r.session_id
where {_SCOPE} and m.started_at >= :since and m.started_at <= :until and m.error_kind is not null
"""


def _safe(value: object, pattern: str) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


async def read_reliability_report(
    session: AsyncSession, since: datetime, until: datetime, *, principal: Principal
) -> ReliabilityReport:
    """Read aggregates inside the caller's transaction; never select error messages."""

    if (
        since.tzinfo is None
        or until.tzinfo is None
        or not timedelta(0) < until - since <= timedelta(days=90)
    ):
        raise ValueError("Reliability windows must be aware, positive and at most ninety days.")
    parameters = {
        "tenant": principal.tenant_id,
        "principal": principal.principal_id,
        "since": since,
        "until": until,
    }

    async def rows(statement: str) -> list[Any]:
        return list((await session.execute(text(statement), parameters)).all())

    [totals] = await rows(_TOTALS)
    [queue] = await rows(_QUEUE)
    failures = await rows(_FAILURES)
    [tools] = await rows(_TOOLS)
    [models] = await rows(_MODELS)
    outcomes = totals.completed + totals.failed
    return ReliabilityReport(
        since=since,
        until=until,
        completed=totals.completed,
        failed=totals.failed,
        cancelled=totals.cancelled,
        internal_errors=totals.internal_errors,
        completion_rate=totals.completed / outcomes if outcomes else None,
        p95_completion_seconds=None if totals.p95 is None else max(0, float(totals.p95)),
        queued=queue.queued,
        running=queue.running,
        waiting=queue.waiting,
        oldest_ready_queue_seconds=max(0, float(queue.oldest or 0)),
        stale_leases=queue.stale,
        tool_failures=tools.failures,
        nonfatal_tool_failures=tools.nonfatal,
        model_failures=models.failures,
        nonfatal_model_failures=models.nonfatal,
        failures=[
            FailureGroup(
                reason=row.reason if row.reason in FailureReason._value2member_map_ else "unknown",
                error_class=_safe(row.error_class, r"[A-Za-z_][A-Za-z0-9_]{0,127}") or "Unknown",
                fingerprint=_safe(row.fingerprint, r"[0-9a-f]{64}"),
                release_id=_safe(row.release_id, r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
                count=row.count,
            )
            for row in failures[:50]
        ],
        failure_groups_truncated=len(failures) > 50,
    )


async def reliability_report(
    sessions: async_sessionmaker[AsyncSession],
    since: datetime,
    until: datetime,
    *,
    principal: Principal,
) -> ReliabilityReport:
    """One consistent, read-only snapshot, with ten-second statement bounds."""

    async with sessions() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '10s'"))
        return await read_reliability_report(session, since, until, principal=principal)
