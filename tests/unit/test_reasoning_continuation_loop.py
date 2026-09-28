"""A reasoning provider's continuation round-trips through the loop unchanged.

model-gateway.md: continuation state has one requirement, round-trip it
unchanged, attached in the provider's own slot and never rendered as prompt
text. The loop keeps it only for the tool turn it accompanied and a terminal
run drops it. The fake provider emits it only when a scripted turn carries
``provider_reasoning_payload``; without that, these paths are unreachable in
fast tests.
"""

from __future__ import annotations

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.messages import (
    FakeModelScript,
    ModelRequest,
    ProviderReasoningItem,
    ScriptedTurn,
    TextPart,
    ToolCallItem,
)
from agent_core.domain.runs import RunStatus
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _calc, _requests, _settings, _tool_turn

PAYLOAD = {"id": "rs_opaque_1", "summary": [], "encrypted_content": "gAAAA-opaque"}


def _reasoning_items(request: ModelRequest) -> list[ProviderReasoningItem]:
    return [item for item in request.conversation if isinstance(item, ProviderReasoningItem)]


def _rendered_text(request: ModelRequest) -> str:
    return "\n".join(
        part.text
        for item in request.conversation
        for part in getattr(item, "content", [])
        if isinstance(part, TextPart)
    )


async def test_a_tool_turns_reasoning_is_replayed_unchanged_before_its_call() -> None:
    reasoning_turn = _tool_turn(_calc("17 * 23", "call-with-reasoning"))
    reasoning_turn = reasoning_turn.model_copy(update={"provider_reasoning_payload": PAYLOAD})
    script = FakeModelScript(turns=[reasoning_turn, ScriptedTurn(text="391")])

    async with build(settings=_settings(), script=script, clock=FixedClock(NOW)) as app:
        run_id = await app.runs.submit("multiply with care")
        run = await app.runs.wait_terminal(run_id)
        requests = _requests(app)
        async with app.uow_factory() as uow:
            terminal = await uow.checkpoints.latest(run_id)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert _reasoning_items(requests[0]) == []
    [replayed] = _reasoning_items(requests[1])
    assert replayed.provider_payload == PAYLOAD
    conversation = requests[1].conversation
    position = conversation.index(replayed)
    following = conversation[position + 1]
    assert isinstance(following, ToolCallItem)
    assert following.call_id == "call-with-reasoning"
    assert "gAAAA-opaque" not in _rendered_text(requests[1])
    assert terminal is not None
    assert terminal.provider_continuation is None


async def test_a_later_tool_turn_without_reasoning_drops_the_earlier_continuation() -> None:
    first = _tool_turn(_calc("1 + 1", "reasoned")).model_copy(
        update={"provider_reasoning_payload": PAYLOAD}
    )
    script = FakeModelScript(
        turns=[first, _tool_turn(_calc("2 + 2", "plain")), ScriptedTurn(text="4")]
    )

    async with build(settings=_settings(), script=script, clock=FixedClock(NOW)) as app:
        run_id = await app.runs.submit("add twice")
        run = await app.runs.wait_terminal(run_id)
        requests = _requests(app)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert [len(_reasoning_items(request)) for request in requests] == [0, 1, 0]
