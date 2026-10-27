"""ADR-0258: a long tool loop can keep what truncation takes from it.

The Cockpit research run of 2026-10-26 made 111 fetches. From step 8 its
oldest results were truncated to pointers that no tool reads back, and a
result of the current run had no event number, so its pointer read
``event:pending`` and a working-state fact could not cite it. The model
fetched the same pages again in three cycles until the identical-call guard
forced an answer, and the run saved nothing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.events import TOOL_RESULT_EVENTS, EventEnvelope
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
from agent_core.runtime.loop import with_event_sequences
from tests.gates.test_runtime_tool_budget import _requests, _settings, _tool_turn
from tests.unit.test_browser_recovery import InterruptedBrowser, navigation
from tests.unit.test_config import base_environment
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


def _results(request: ModelRequest) -> list[ToolResultItem]:
    return [item for item in request.conversation if isinstance(item, ToolResultItem)]


def _recorded_by(events: list[EventEnvelope]) -> dict[str, int]:
    """The sequence of the event that recorded each call's result."""

    return {
        event.payload["call_id"]: event.sequence
        for event in events
        if event.event_type in TOOL_RESULT_EVENTS
    }


async def test_a_browser_recovery_read_carries_its_event() -> None:
    # The runtime's own recovery read is appended outside the model's batch.
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[navigation(), ScriptedTurn(text="Report evidence.")]),
        sequential_ids=True,
        enabled_tools=["browser.navigate", "browser.observe"],
        browser_provider_override=InterruptedBrowser(),
    ) as composition:
        run_id = await composition.runs.submit("Read the account.")
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)
        recorded = _recorded_by(await composition.runs.events(run_id))

    assert run.status is RunStatus.COMPLETED, run.failure
    results = _results(requests[-1])
    assert [item.call_id.startswith("browser-recovery:") for item in results] == [False, True]
    assert [item.source_event_sequence for item in results] == [
        recorded[item.call_id] for item in results
    ]


async def test_a_batch_resumed_after_approval_carries_its_events() -> None:
    script = FakeModelScript(
        turns=[
            _tool_turn(
                ScriptedToolCall(
                    name="demo.external_write",
                    arguments={"destination": "demo", "content": "hello"},
                )
            ),
            ScriptedTurn(text="The approved write finished."),
        ]
    )
    async with build(settings=_settings(), script=script) as composition:
        run_id = await composition.runs.submit("Write hello to the demo destination.")
        assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await composition.runs.wait_terminal(run_id)
        requests = _requests(composition)
        recorded = _recorded_by(await composition.runs.events(run_id))

    assert run.status is RunStatus.COMPLETED, run.failure
    [result] = _results(requests[-1])
    assert result.source_event_sequence == recorded[result.call_id]


async def test_a_call_id_recorded_twice_gets_no_event() -> None:
    """A result names its event only when its call identifier picks one out.

    Duplicate identifiers already fail the run before its next model request,
    but the batch is checkpointed first; the later event must not be written
    onto both results.
    """

    run_id, session_id = uuid4(), uuid4()

    def recorded(sequence: int, call_id: str) -> EventEnvelope:
        return EventEnvelope(
            id=sequence,
            session_id=session_id,
            run_id=run_id,
            sequence=sequence,
            event_type="tool.call.completed",
            payload_schema_version=1,
            actor_type="runtime",
            actor_id=None,
            payload={"call_id": call_id},
            trace_id=None,
            created_at=datetime(2026, 10, 27, tzinfo=UTC),
        )

    events = [recorded(11, "twice"), recorded(12, "twice"), recorded(13, "once")]

    class _Events:
        async def list_after(self, *_args: Any, **_kwargs: Any) -> list[EventEnvelope]:
            return events

    class _UnitOfWork:
        events = _Events()

        async def __aenter__(self) -> _UnitOfWork:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    context = SimpleNamespace(
        uow_factory=_UnitOfWork,
        run=SimpleNamespace(id=run_id, session_id=session_id),
        checkpoint=SimpleNamespace(last_event_sequence=10),
        principal=None,
    )
    results = [
        ToolResultItem(call_id=call_id, content=[TextPart(text=call_id)])
        for call_id in ("twice", "twice", "once")
    ]

    attributed = await with_event_sequences(cast(Any, context), results)

    assert [item.source_event_sequence for item in attributed] == [None, None, 13]
