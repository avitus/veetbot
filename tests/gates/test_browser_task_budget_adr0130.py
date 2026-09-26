"""ADR-0130: a chat bound to a website profile gets a budget for a whole lesson.

On the production-shaped roster of the ADR-0123 gate, with the hosted browser
provider: a bound chat defines every browser tool and runs under the
browser-task limits; it exceeds its USD 30 cost limit by at most the one call
in flight; the call that reaches the cost reserve and asks for a tool ends
the run; an unbound chat is unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx

from agent_core.api import create_app
from agent_core.bootstrap import Composition
from agent_core.domain.browser import BrowserProfile, BrowserProfileStatus
from agent_core.domain.context import ContextPlan
from agent_core.domain.messages import (
    ModelUsage,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import FailureReason, Run, RunLimits, RunStatus
from tests.gates.test_deferred_tools_adr0123 import (
    DISCOVERED_READS,
    START_CALL,
    _production,
    _session_events,
)

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000a7")
HOSTED_BROWSER = {
    "BROWSER_PROVIDER": "hosted",
    "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
    "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
}
BROWSER_TOOLS = frozenset({"browser.navigate", "browser.observe", "browser.act"})
BROWSER_TASK_LIMITS = RunLimits(
    max_steps=160,
    max_model_calls=120,
    max_tool_calls=160,
    max_cost=Decimal("30"),
    synthesis_reserve_model_calls=2,
    synthesis_reserve_tool_calls=4,
    synthesis_reserve_cost=Decimal("3"),
)
SEEDED_AT = datetime(2026, 9, 25, 12, tzinfo=UTC)


async def _bound_session(app: Composition) -> UUID:
    owner = app.principal
    async with app.uow_factory() as uow:
        await uow.browser_profiles.create(
            BrowserProfile(
                id=PROFILE_ID,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                provider_name="hosted-isolated",
                provider_ref="opaque-provider-reference",
                allowed_origins=("https://www.example.org",),
                status=BrowserProfileStatus.READY,
                generation=1,
                encryption_key_version="key-v1",
                created_at=SEEDED_AT,
                updated_at=SEEDED_AT,
            )
        )
    created = await app.services.sessions.create(
        owner, "general", {}, browser_profile_id=PROFILE_ID
    )
    return created.id


def _time_turn(cost: str, call_id: str) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ScriptedToolCall(name="system.current_time", arguments={}, call_id=call_id)],
        stop_reason=StopReason.TOOL_USE,
        usage=ModelUsage(cost=Decimal(cost)),
    )


async def _bound_run(
    tmp_path: Path, turns: list[ScriptedTurn]
) -> tuple[Run, ContextPlan | None, list[str]]:
    _factory, context = await _production(tmp_path, turns, extra_environment=HOSTED_BROWSER)
    async with context as app:
        session_id = await _bound_session(app)
        run_id = await app.runs.submit("Do one lesson.", session_id)
        run = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        plan = await app.executor._context_planner.current(session_id)
        events = [event.event_type for event in await _session_events(app, session_id)]
    return run, plan, events


async def test_bound_production_chat_defines_every_browser_tool(tmp_path: Path) -> None:
    run, plan, _events = await _bound_run(
        tmp_path, [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)]
    )

    assert run.status is RunStatus.COMPLETED, run.failure
    assert plan is not None
    defined = set(plan.tool_names)
    assert BROWSER_TOOLS | {"tool.call"} <= defined
    assert len(plan.tool_names) == 30
    assert plan.skipped_tool_names == ()
    moved = DISCOVERED_READS - defined
    assert moved == {
        "mcp.gmail_work_read.get_thread",
        "mcp.gmail_work_read.list_labels",
        "mcp.gmail_work_read.search_threads",
    }
    assert moved | {START_CALL} <= set(plan.deferred_tool_names)


async def test_bound_production_chat_runs_under_the_browser_task_limits(tmp_path: Path) -> None:
    run, _plan, _events = await _bound_run(
        tmp_path, [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)]
    )

    assert run.limits == BROWSER_TASK_LIMITS


async def test_bound_run_view_shows_the_cost_limit(tmp_path: Path) -> None:
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment=HOSTED_BROWSER,
    )
    async with context as app:
        session_id = await _bound_session(app)
        run_id = await app.runs.submit("Do one lesson.", session_id)
        await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 43100)),
            base_url="http://agent.test",
        ) as client:
            response = await client.get(f"/v1/runs/{run_id}")

    assert response.status_code == 200, response.text
    assert response.json()["limits"]["max_cost_usd"] == "30"
    assert response.json()["limits"]["max_steps"] == 160


async def test_a_bound_run_exceeds_its_cost_limit_by_at_most_one_call(tmp_path: Path) -> None:
    """The fourth call starts at USD 24, below the limit; recording it ends the run."""

    run, _plan, events = await _bound_run(
        tmp_path,
        [
            _time_turn("8", "time-1"),
            _time_turn("8", "time-2"),
            _time_turn("8", "time-3"),
            ScriptedTurn(
                text="Done.", stop_reason=StopReason.END_TURN, usage=ModelUsage(cost=Decimal("8"))
            ),
        ],
    )

    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.reason is FailureReason.BUDGET_EXCEEDED
    assert run.usage.cost == Decimal("32")
    assert run.model_call_count == 4
    assert events.count("model.request.started") == 4


async def test_a_bound_run_that_reaches_the_cost_reserve_while_acting_ends_budget_exceeded(
    tmp_path: Path,
) -> None:
    """ADR-0078 decision 3 as implemented (0130-design F19)."""

    run, _plan, _events = await _bound_run(
        tmp_path,
        [
            _time_turn("8", "time-1"),
            _time_turn("8", "time-2"),
            _time_turn("8", "time-3"),
            _time_turn("4", "time-4"),
            ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
        ],
    )

    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.reason is FailureReason.BUDGET_EXCEEDED
    assert run.failure.details == {"synthesis_reserve": "cost"}
    assert run.usage.cost == Decimal("28")


async def test_unbound_production_chat_is_unchanged(tmp_path: Path) -> None:
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment=HOSTED_BROWSER,
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("What can you do for me?", session_id)
        run = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        plan = await app.executor._context_planner.current(session_id)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert plan is not None
    assert not set(plan.tool_names) & BROWSER_TOOLS
    assert len(plan.tool_names) == 30
    assert set(plan.tool_names) >= DISCOVERED_READS
    assert (run.limits.max_steps, run.limits.max_model_calls, run.limits.max_tool_calls) == (
        32,
        24,
        64,
    )
    assert run.limits.max_cost is None
