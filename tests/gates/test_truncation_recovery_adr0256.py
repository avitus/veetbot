"""ADR-0256: a long tool loop can keep what truncation takes from it.

The Cockpit research run of 2026-10-26 made 111 fetches. From step 8 its
oldest results were truncated to pointers that no tool reads back, and a
result of the current run had no event number, so its pointer read
``event:pending`` and a working-state fact could not cite it. The model
fetched the same pages again in three cycles until the identical-call guard
forced an answer, and the run saved nothing.
"""

from __future__ import annotations

import re

from agent_core.bootstrap import build
from agent_core.domain.messages import (
    FakeModelScript,
    ModelRequest,
    ScriptedToolCall,
    ScriptedTurn,
    TextPart,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunLimits, RunStatus
from tests.gates.test_runtime_tool_budget import _requests, _settings, _tool_turn
from tests.unit.test_web_tools import FakeWebProvider

LIMITS = RunLimits(max_steps=20, max_model_calls=20, max_tool_calls=40)
# Small enough to stay inline, so a truncated page points at its event rather
# than at a captured artifact; twenty of them pass the tool-result budget.
PAGE = "Branch evidence. " * 170


def _pointers(request: ModelRequest) -> list[str]:
    return [
        match
        for item in request.conversation
        if isinstance(item, ToolResultItem)
        for part in item.content
        if isinstance(part, TextPart)
        for match in re.findall(r"full content at (event:\w+)", part.text)
    ]


def _notes(request: ModelRequest) -> list[str]:
    return [
        part.text
        for item in request.conversation
        if isinstance(item, UserMessage) and item.trust is TrustLevel.PLATFORM
        for part in item.content
        if isinstance(part, TextPart) and part.text.startswith("Tool-result note")
    ]


async def test_live_results_carry_the_event_a_fact_cites() -> None:
    provider = FakeWebProvider(page_content=PAGE)
    turns = [
        _tool_turn(
            *(
                ScriptedToolCall(
                    name="web.fetch", arguments={"url": f"https://example.org/branch-{step}-{n}"}
                )
                for n in range(2)
            )
        )
        for step in range(12)
    ]
    turns.append(ScriptedTurn(text="Here is what each branch holds."))
    async with build(
        settings=_settings(),
        script=FakeModelScript(turns=turns),
        limits=LIMITS,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Which of my branches are ahead of main?")
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, composition.principal)

    assert run.status is RunStatus.COMPLETED, run.failure
    completed = {event.sequence for event in events if event.event_type == "tool.call.completed"}
    last = requests[-1]
    pointers = _pointers(last)
    assert pointers, "the fixture must actually exercise tool-result truncation"
    assert {pointer.removeprefix("event:") for pointer in pointers} <= {
        str(sequence) for sequence in completed
    }
    assert not _notes(requests[0])
    [note] = _notes(last)
    listed = {int(value) for value in re.findall(r"event (\d+) \(web\.fetch\)", note)}
    shown = {
        item.source_event_sequence
        for item in last.conversation
        if isinstance(item, ToolResultItem)
        and not any(
            isinstance(part, TextPart) and "[tool result truncated:" in part.text
            for part in item.content
        )
    }
    assert listed
    assert listed <= completed
    assert listed <= shown
