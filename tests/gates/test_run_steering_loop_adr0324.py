"""A working run reads a held follow-up at its next step (ADR-0324)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import agent_core.runtime.loop as runtime_loop
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.bootstrap import Composition, build
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedTurn,
    TextPart,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import SubmitResult, TextContentBlock
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _calc, _requests, _settings, _tool_turn

FOLLOW_UP = "Use metric units."


def _send_follow_up_after_first_tool_batch(
    monkeypatch: pytest.MonkeyPatch, composition: Composition
) -> list[SubmitResult]:
    """Submit one follow-up through the public service once the first batch returns."""

    held: list[SubmitResult] = []
    original = runtime_loop.checkpoint

    async def checkpoint(context: Any, trigger: str, **options: Any) -> None:
        await original(context, trigger, **options)
        if trigger == "tool_call" and not held:
            held.append(
                await composition.services.runs.submit(
                    composition.principal,
                    context.run.session_id,
                    [TextContentBlock(text=FOLLOW_UP)],
                    None,
                    None,
                )
            )

    monkeypatch.setattr(runtime_loop, "checkpoint", checkpoint)
    return held


async def test_a_follow_up_is_read_at_the_next_step(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _tool_turn(_calc("1 + 1", "a")).model_copy(
        # The run holds a reasoning continuation when the follow-up arrives.
        update={"provider_reasoning_payload": {"encrypted_content": "opaque"}}
    )
    script = FakeModelScript(
        turns=[first, ScriptedTurn(text="Two metres.", context_contains=FOLLOW_UP)]
    )
    async with build(
        settings=replace(_settings(), run_steering_enabled=True),
        script=script,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        held = _send_follow_up_after_first_tool_batch(monkeypatch, composition)
        run_id = await composition.runs.submit("How long is it?")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
        requests = _requests(composition)
        async with composition.uow_factory() as uow:
            inbox = await uow.pending_inputs.list_for_run(run_id, composition.principal)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert [result.run_id for result in held] == [run_id]
    assert inbox == []

    # The follow-up is one ordinary user message, after the batch it waited for.
    messages = [event for event in events if event.event_type == "user.message.created"]
    assert len(messages) == 2
    follow_up = messages[1]
    assert [part["text"] for part in follow_up.payload["content"]] == [FOLLOW_UP]
    assert follow_up.payload["pending_input_id"] == str(held[0].pending_input_id)
    assert follow_up.actor_type == "principal"
    kinds = [event.event_type for event in events]
    assert kinds.index("tool.call.completed") < events.index(follow_up)
    assert events.index(follow_up) < kinds.index("assistant.message.completed")

    # The model's next request ends with it, after the tool result.
    conversation = requests[1].conversation
    assert isinstance(conversation[-1], UserMessage)
    (part,) = conversation[-1].content
    assert isinstance(part, TextPart)
    # It carries the owner's trust, in the same envelope as an initiating message.
    assert FOLLOW_UP in part.text and 'trust="user" source="principal"' in part.text
    # Only the context engine's fresh platform rows sit between the batch and it.
    result_at = next(
        index
        for index, item in enumerate(conversation)
        if isinstance(item, ToolResultItem) and item.call_id == "a"
    )
    between = conversation[result_at + 1 : -1]
    assert all(
        isinstance(item, UserMessage) and item.trust is not TrustLevel.USER for item in between
    )
