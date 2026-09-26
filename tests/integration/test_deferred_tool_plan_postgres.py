"""A Chat plan with deferred tools survives PostgreSQL persistence (ADR-0123).

The builder re-renders a plan's prefix from its persisted specifications and
compares the hash. PostgreSQL jsonb keeps no object key order, so an index
entry that listed parameters in key order rendered new bytes after the plan
event was written, and every new chat failed its first message.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

import agent_core.context.rendering as rendering
from agent_core.bootstrap import Composition, build
from agent_core.context.planner import EventContextPlanner
from agent_core.domain.context import ContextPlan
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.tools import ToolSpec
from agent_core.runtime.worker import DurableWorker
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings

REPLY = "Set the snack table by the door."


def _composition() -> AbstractAsyncContextManager[Composition]:
    # The schedule flag pair gives the default agent its deferred lifecycle tools.
    settings = replace(database_settings(), schedule_api_enabled=True, schedule_worker_enabled=True)
    return build(
        settings=settings,
        storage="postgres",
        script=FakeModelScript(turns=[ScriptedTurn(text=REPLY)]),
        fixed_clock_at=NOW,
    )


async def _answer(composition: Composition, session_id: UUID) -> Run:
    run_id = await composition.runs.submit("My daughter is hosting a dance party.", session_id)
    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=composition.clock,
        worker_id="deferred-index-worker",
    )
    assert await worker.run_once()
    return await composition.runs.get(run_id)


def _key_order_line(spec: ToolSpec) -> str:
    """The index entry as first released: parameters in the schema's key order."""

    properties = spec.input_schema.get("properties")
    required = set(spec.input_schema.get("required") or ())
    parameters = (
        [name if name in required else f"{name}?" for name in properties]
        if isinstance(properties, dict)
        else []
    )
    return f"- {spec.name}({', '.join(parameters)}): {rendering._first_sentence(spec.description)}"


async def test_a_new_chat_with_deferred_tools_answers_its_first_message() -> None:
    async with _composition() as composition:
        session_id = await composition.sessions.create()
        run = await _answer(composition, session_id)
        planner = cast(EventContextPlanner, composition.executor._context_planner)
        plan = await planner.current(session_id)

    assert plan is not None and "schedule.update" in plan.deferred_tool_names
    assert run.failure is None
    assert run.status is RunStatus.COMPLETED
    assert run.final_message == REPLY


async def test_a_chat_planned_by_the_key_order_index_recovers_on_its_next_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _composition() as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(session_id, composition.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)
        planner = cast(EventContextPlanner, composition.executor._context_planner)
        with monkeypatch.context() as released:
            released.setattr(rendering, "_index_line", _key_order_line)
            stale = await planner.plan(
                session, agent, composition.principal, composition.executor._resolved_model
            )

    # The next message reaches a new process, as after a deploy, which reads the
    # stale plan back from PostgreSQL rather than from a planner's cache.
    async with _composition() as restarted:
        run = await _answer(restarted, session_id)
        planner = cast(EventContextPlanner, restarted.executor._context_planner)
        repaired = await planner.current(session_id)
        async with restarted.uow_factory() as uow:
            rotation = await uow.events.latest_before(
                session_id, (1 << 63) - 1, "context.epoch.rotated", restarted.principal
            )

    assert run.status is RunStatus.COMPLETED
    assert run.final_message == REPLY
    assert repaired is not None and repaired.epoch == stale.epoch + 1
    assert repaired.deferred_tool_names == stale.deferred_tool_names
    assert rotation is not None and rotation.payload["reason"] == "agent_prefix_changed"
    assert ContextPlan.model_validate(rotation.payload["plan"]) == repaired
