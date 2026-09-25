"""Read-only Chat latency aggregates over `events` and `model_calls` (ADR-0131).

Every statement selects durations, counts, token sums, event types, tool and
server identifiers, and session-metadata key names. None selects message,
argument, result, title or metadata values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_core.observability.latency import (
    ChatLatencyReport,
    Distribution,
    ModelTiming,
    NamedDistribution,
    QueueWait,
)

# Completed top-level Chat turns that never waited for a person.
_CHAT_RUNS = """
chat_runs as (
  select r.id, r.session_id, r.usage
  from runs r join sessions s on s.id = r.session_id
  where s.tenant_id = :tenant and s.metadata = '{}'::jsonb and r.parent_run_id is null
    and lower(r.status) = 'completed' and r.created_at > :since
    and not exists (
      select 1 from events w where w.run_id = r.id
        and w.event_type in ('run.waiting_for_approval', 'run.waiting_for_user'))
),
run_events as (
  select e.run_id, e.session_id, e.event_type, e.created_at, e.payload
  from events e join chat_runs r on r.id = e.run_id
)
"""

_DISTRIBUTION = """
count(v) as n,
percentile_cont(0.5) within group (order by v) as p50,
percentile_cont(0.9) within group (order by v) as p90,
max(v) as maximum
"""

_PHASES = f"""
with {_CHAT_RUNS},
model_time as (
  select started.run_id, count(*) as calls,
         sum(extract(epoch from done.created_at - started.created_at)) as seconds
  from run_events started join run_events done on done.run_id = started.run_id
    and done.event_type = 'model.response.completed'
    and done.payload->>'attempt_id' = started.payload->>'attempt_id'
  where started.event_type = 'model.request.started'
  group by started.run_id
),
tool_time as (
  select started.run_id, sum(extract(epoch from done.created_at - started.created_at)) as seconds
  from run_events started join run_events done on done.run_id = started.run_id
    and done.payload->>'call_id' = started.payload->>'call_id'
    and done.event_type in ('tool.call.completed', 'tool.call.failed', 'tool.call.uncertain')
  where started.event_type = 'tool.call.started'
  group by started.run_id
),
per_run as (
  select run_id,
    extract(epoch from min(created_at) filter (where event_type = 'assistant.message.completed')
      - min(created_at) filter (where event_type = 'run.queued')) as turn,
    extract(epoch from min(created_at) filter (where event_type = 'run.claimed')
      - min(created_at) filter (where event_type = 'run.queued')) as queue,
    extract(epoch from min(created_at) filter (where event_type = 'model.request.started')
      - min(created_at) filter (where event_type = 'run.started')) as setup
  from run_events group by run_id
),
metrics as (
  select name, v from per_run
  left join model_time on model_time.run_id = per_run.run_id
  left join tool_time on tool_time.run_id = per_run.run_id,
  lateral (values ('turn', per_run.turn), ('queue', per_run.queue), ('setup', per_run.setup),
                  ('model', model_time.seconds), ('tools', tool_time.seconds),
                  ('model_calls', model_time.calls::float8)) as metric(name, v)
)
select name, {_DISTRIBUTION} from metrics where v is not null group by name
"""

_SETUP = f"""
with {_CHAT_RUNS},
window_bounds as (
  select run_id, session_id,
    min(created_at) filter (where event_type = 'run.started') as started,
    min(created_at) filter (where event_type = 'model.request.started') as first_request
  from run_events group by run_id, session_id
),
setup as (
  select extract(epoch from first_request - started) as v,
    exists (
      select 1 from events plan where plan.session_id = window_bounds.session_id
        and plan.run_id is null
        and plan.event_type in ('context.plan.created', 'context.epoch.rotated')
        and plan.created_at between window_bounds.started and window_bounds.first_request
    ) as first_turn
  from window_bounds where started is not null and first_request is not null
)
select case when first_turn then 'first turn' else 'follow-up' end as name, {_DISTRIBUTION}
from setup group by 1
"""

_TOOLS = f"""
with {_CHAT_RUNS},
calls as (
  select started.payload->>'name' as name,
         extract(epoch from done.created_at - started.created_at) as v
  from run_events started join run_events done on done.run_id = started.run_id
    and done.payload->>'call_id' = started.payload->>'call_id'
    and done.event_type in ('tool.call.completed', 'tool.call.failed', 'tool.call.uncertain')
  where started.event_type = 'tool.call.started'
)
select name, {_DISTRIBUTION} from calls group by name
"""

_MODELS = f"""
with {_CHAT_RUNS},
attempts as (
  select mc.provider || '/' || mc.model as model,
         requested.payload->>'reasoning_effort' as effort,
         extract(epoch from mc.finished_at - mc.started_at) as duration,
         (completed.payload->>'time_to_first_text_ms')::float8 / 1000.0 as first_text,
         mc.input_tokens, mc.output_tokens, mc.cached_input_tokens
  from model_calls mc join chat_runs r on r.id = mc.run_id
  left join events requested on requested.run_id = mc.run_id
    and requested.event_type = 'model.request.started'
    and requested.payload->>'attempt_id' = mc.attempt_id::text
  left join events completed on completed.run_id = mc.run_id
    and completed.event_type = 'model.response.completed'
    and completed.payload->>'attempt_id' = mc.attempt_id::text
  where mc.finished_at is not null
)
select model, effort, count(*) as calls,
  count(duration) as duration_n,
  percentile_cont(0.5) within group (order by duration) as duration_p50,
  percentile_cont(0.9) within group (order by duration) as duration_p90,
  max(duration) as duration_maximum,
  count(first_text) as first_text_n,
  percentile_cont(0.5) within group (order by first_text) as first_text_p50,
  percentile_cont(0.9) within group (order by first_text) as first_text_p90,
  max(first_text) as first_text_maximum,
  round(avg(input_tokens)) as input_tokens,
  round(avg(output_tokens)) as output_tokens,
  100.0 * sum(cached_input_tokens) / nullif(sum(input_tokens), 0) as cached_percent
from attempts group by model, effort
"""

_MCP_CONNECTIONS = f"""
with connections as (
  select e.payload->>'server_id' as name, (e.payload->>'duration_ms')::float8 / 1000.0 as v
  from events e join sessions s on s.id = e.session_id
  where s.tenant_id = :tenant and s.metadata = '{{}}'::jsonb and e.created_at > :since
    and e.event_type = 'mcp.server.connected'
    and jsonb_typeof(e.payload->'duration_ms') = 'number'
)
select name, {_DISTRIBUTION} from connections group by name
"""

_MCP_PINS = """
select count(*) from events e join sessions s on s.id = e.session_id
where s.tenant_id = :tenant and s.metadata = '{}'::jsonb and e.created_at > :since
  and e.event_type = 'mcp.server.pinned'
"""

_QUEUE = f"""
with waits as (
  select r.priority,
    coalesce(
      (select string_agg(key, ',' order by key) from jsonb_object_keys(s.metadata) as key
        where key ~ '^[a-z][a-z0-9_]*$'),
      'chat') as kind,
    extract(epoch from claimed.at - queued.created_at) as v
  from runs r join sessions s on s.id = r.session_id
  join events queued on queued.run_id = r.id and queued.event_type = 'run.queued'
  join lateral (
    select min(x.created_at) as at from events x
    where x.run_id = r.id and x.event_type = 'run.claimed'
  ) as claimed on claimed.at is not null
  where s.tenant_id = :tenant and r.created_at > :since
)
select priority, kind, {_DISTRIBUTION} from waits group by priority, kind
"""

_TOTALS = f"""
with {_CHAT_RUNS}
select count(*) as turns,
  100.0 * sum((usage->>'cached_input_tokens')::bigint)
    / nullif(sum((usage->>'input_tokens')::bigint), 0) as cached_percent,
  100.0 * sum(coalesce((usage->>'reasoning_tokens')::bigint, 0))
    / nullif(sum((usage->>'output_tokens')::bigint), 0) as reasoning_percent
from chat_runs
"""


def _seconds(value: Any) -> float | None:
    return None if value is None else round(float(value), 3)


def _percent(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


def _distribution(row: Any, prefix: str = "") -> Distribution:
    return Distribution(
        count=int(getattr(row, f"{prefix}n")),
        p50=_seconds(getattr(row, f"{prefix}p50")),
        p90=_seconds(getattr(row, f"{prefix}p90")),
        maximum=_seconds(getattr(row, f"{prefix}maximum")),
    )


def _named(row: Any) -> NamedDistribution:
    return NamedDistribution(name=str(row.name), **_distribution(row).model_dump())


async def chat_latency_report(
    sessions: async_sessionmaker[AsyncSession], since: datetime, *, tenant_id: str
) -> ChatLatencyReport:
    """Aggregate recent Chat turns in one read-only transaction."""

    parameters = {"tenant": tenant_id, "since": since}
    async with sessions() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))

        async def rows(statement: str) -> list[Any]:
            return list((await session.execute(text(statement), parameters)).all())

        totals = (await rows(_TOTALS))[0]
        phases = sorted((_named(row) for row in await rows(_PHASES)), key=lambda item: item.name)
        setup = sorted((_named(row) for row in await rows(_SETUP)), key=lambda item: item.name)
        tools = sorted((_named(row) for row in await rows(_TOOLS)), key=lambda item: -item.count)
        connections = sorted(
            (_named(row) for row in await rows(_MCP_CONNECTIONS)), key=lambda item: item.name
        )
        pins = (await rows(_MCP_PINS))[0][0]
        models = [
            ModelTiming(
                model=str(row.model),
                reasoning_effort=row.effort,
                calls=int(row.calls),
                duration=_distribution(row, "duration_"),
                first_text=_distribution(row, "first_text_"),
                average_input_tokens=int(row.input_tokens or 0),
                average_output_tokens=int(row.output_tokens or 0),
                cached_input_percent=_percent(row.cached_percent),
            )
            for row in await rows(_MODELS)
        ]
        queue = [
            QueueWait(
                priority=int(row.priority), session_kind=str(row.kind), wait=_distribution(row)
            )
            for row in await rows(_QUEUE)
        ]
    return ChatLatencyReport(
        since=since,
        turns=int(totals.turns),
        phases=phases,
        setup=setup,
        models=sorted(models, key=lambda item: -item.calls),
        tools=tools,
        mcp_connections=connections,
        mcp_pins_reused=int(pins),
        queue=sorted(queue, key=lambda item: (item.priority, item.session_kind)),
        cached_input_percent=_percent(totals.cached_percent),
        reasoning_output_percent=_percent(totals.reasoning_percent),
    )
