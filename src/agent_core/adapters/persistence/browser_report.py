"""Owner-scoped, read-only projection of bounded browser diagnostic metadata."""

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_core.domain.agents import Principal
from agent_core.observability.browser import (
    MAX_BROWSER_DIAGNOSTIC_BYTES,
    MAX_BROWSER_REPORT_OPERATIONS,
    BrowserOperationReport,
    BrowserReportSample,
    BrowserTool,
    TerminalOutcome,
    aggregate_browser_report,
)

_SAMPLES = """
select e.payload->>'name' as tool,
       case e.event_type
         when 'tool.call.completed' then 'completed'
         when 'tool.call.failed' then 'failed'
         when 'tool.call.uncertain' then 'uncertain'
       end as outcome,
       e.payload ? 'browser_diagnostics' as diagnostics_present,
       case when octet_length((e.payload->'browser_diagnostics')::text) <= :maximum_bytes
         then (e.payload->'browser_diagnostics')::text else null end as diagnostics
from events e join sessions s on s.id=e.session_id
where s.tenant_id=:tenant and s.principal_id=:principal
  and e.created_at >= :since and e.created_at <= :until
  and e.event_type in ('tool.call.completed', 'tool.call.failed', 'tool.call.uncertain')
  and e.payload->>'name' in ('browser.navigate', 'browser.observe', 'browser.act', 'browser.upload')
order by e.created_at desc, e.id desc
limit :maximum_rows
"""


async def browser_operation_report(
    sessions: async_sessionmaker[AsyncSession],
    since: datetime,
    until: datetime,
    *,
    principal: Principal,
) -> BrowserOperationReport:
    """Aggregate one bounded snapshot without selecting event content or changing state."""
    if (
        since.tzinfo is None
        or until.tzinfo is None
        or not timedelta(0) < until - since <= timedelta(days=90)
    ):
        raise ValueError("Browser report windows must be aware, positive and at most ninety days.")
    parameters = {
        "tenant": principal.tenant_id,
        "principal": principal.principal_id,
        "since": since,
        "until": until,
        "maximum_rows": MAX_BROWSER_REPORT_OPERATIONS + 1,
        "maximum_bytes": MAX_BROWSER_DIAGNOSTIC_BYTES,
    }
    async with sessions() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '10s'"))
        rows = (await session.execute(text(_SAMPLES), parameters)).all()
    return aggregate_browser_report(
        (
            BrowserReportSample(
                tool=cast(BrowserTool, row.tool),
                outcome=cast(TerminalOutcome, row.outcome),
                diagnostics_present=row.diagnostics_present,
                diagnostics=row.diagnostics,
            )
            for row in rows
        ),
        since=since,
        until=until,
    )
