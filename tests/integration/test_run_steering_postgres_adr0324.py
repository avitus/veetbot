"""Follow-ups under a durable worker and its lease on PostgreSQL (ADR-0324)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import agent_core.runtime.loop as runtime_loop
from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import SubmitResult, TextContentBlock
from agent_core.runtime.worker import DurableWorker
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _calc, _tool_turn
from tests.integration.m2_support import database_settings

FOLLOW_UP = "Use metric units."


async def test_a_leased_run_reads_one_follow_up_and_carries_the_late_one_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = FakeModelScript(
        turns=[
            _tool_turn(_calc("1 + 1", "a")),
            ScriptedTurn(text="Two metres.", context_contains=FOLLOW_UP),
            ScriptedTurn(text="In feet, six and a half.", context_contains="Now in feet."),
        ]
    )
    held: dict[str, SubmitResult] = {}
    original = runtime_loop.checkpoint

    async def checkpoint(context: Any, trigger: str, **options: Any) -> None:
        await original(context, trigger, **options)
        if trigger == "tool_call" and "read" not in held:
            held["read"] = await _follow_up(context.run.session_id, FOLLOW_UP)
        elif trigger == "model_response" and "read" in held and "late" not in held:
            # The final answer is in; the terminal transition must carry this over.
            held["late"] = await _follow_up(context.run.session_id, "Now in feet.")

    async def _follow_up(session_id: Any, text: str) -> SubmitResult:
        result: SubmitResult = await composition.services.runs.submit(
            composition.principal, session_id, [TextContentBlock(text=text)], None, None
        )
        return result

    monkeypatch.setattr(runtime_loop, "checkpoint", checkpoint)
    clock = FixedClock(NOW)
    settings = replace(database_settings(), artifact_root=tmp_path, run_steering_enabled=True)
    async with build(settings=settings, storage="postgres", script=script, clock=clock) as (
        composition
    ):
        first_id = await composition.runs.submit("How long is it?")
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="steering-worker",
        )
        assert await worker.run_once()
        first = await composition.runs.get(first_id)
        async with composition.uow_factory() as uow:
            events = list(await uow.events.list_after(first.session_id, 0, composition.principal))
            assert await uow.pending_inputs.list_for_run(first_id, composition.principal) == []
            queued = await uow.runs.active_for_session(first.session_id, composition.principal)
        assert queued is not None and queued.status is RunStatus.QUEUED
        assert await worker.run_once()
        second = await composition.runs.get(queued.id)

    assert first.status is RunStatus.COMPLETED, first.failure
    assert first.final_message == "Two metres."
    assert held["read"].run_id == held["late"].run_id == first_id
    messages = [event for event in events if event.event_type == "user.message.created"]
    assert [(event.run_id, event.payload.get("pending_input_id")) for event in messages[1:]] == [
        (first_id, str(held["read"].pending_input_id)),
        (queued.id, None),
    ]
    assert messages[2].payload["pending_input_ids"] == [str(held["late"].pending_input_id)]
    assert not [event for event in events if event.event_type == "run.input.dropped"]
    assert second.status is RunStatus.COMPLETED, second.failure
    assert second.final_message == "In feet, six and a half."
