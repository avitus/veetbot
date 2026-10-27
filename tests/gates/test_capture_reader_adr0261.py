"""ADR-0261 gate: Chat reads a long fetched page in sections, still untrusted.

The first turn fetches a filing too large for the inline limit; its marker
names `artifact.read_capture`. The second turn searches the capture, reads two
sections in one step, has a third refused, and then proposes a sandbox command,
which still asks: reading changed nothing about the trust overlay.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from agent_core.bootstrap import build
from agent_core.context.rendering import build_prefix
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.captures import CaptureSections
from agent_core.domain.messages import (
    FakeModelScript,
    FileReferencePart,
    ScriptedToolCall,
    ScriptedTurn,
    TextPart,
    ToolResultItem,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunLimits, RunStatus
from tests.gates.test_runtime_tool_budget import _requests, _settings, _tool_turn
from tests.unit.test_web_tools import FakeWebProvider

READER = "artifact.read_capture"
FILING = "".join(
    f"Item {n}. "
    + ("Results of Operations: revenue rose." if n % 500 == 0 else "Risk factors continue.")
    + "\n"
    for n in range(14_500)
)
LIMITS = RunLimits(max_steps=12, max_model_calls=12, max_tool_calls=24)


def _read(call_id: str, arguments: dict[str, Any]) -> ScriptedToolCall:
    return ScriptedToolCall(
        name="tool.call", arguments={"name": READER, "arguments": arguments}, call_id=call_id
    )


async def _events(app: Any, session_id: UUID) -> list[Any]:
    async with app.uow_factory() as uow:
        return list(await uow.events.list_after(session_id, 0, app.principal))


async def test_a_chat_searches_and_reads_a_long_capture_in_sections() -> None:
    script = FakeModelScript(
        turns=[
            _tool_turn(
                ScriptedToolCall(
                    name="web.fetch", arguments={"url": "https://example.org/s-1"}, call_id="fetch"
                )
            ),
            ScriptedTurn(text="Fetched the filing."),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        limits=LIMITS,
        web_fetch_provider_override=FakeWebProvider(page_content=FILING),
    ) as app:
        session_id = await app.sessions.create()
        first = await app.runs.submit("Fetch the filing at https://example.org/s-1.", session_id)
        assert (await asyncio.wait_for(app.runs.wait_terminal(first), 30)).status is (
            RunStatus.COMPLETED
        )
        events = await _events(app, session_id)
        fetched = next(
            ToolResultItem.model_validate(event.payload["result_item"])
            for event in events
            if event.event_type == "tool.call.completed" and event.payload["name"] == "web.fetch"
        )
        assert fetched.context_content is not None
        marker = fetched.context_content[0]
        assert isinstance(marker, TextPart)
        reference = next(p for p in fetched.context_content if isinstance(p, FileReferencePart))
        artifact = str(reference.artifact_id)
        page = "Provider: fake-web\n\n" + FILING
        sections = CaptureSections(page, artifact_id=reference.artifact_id, tool="web.fetch")
        assert (
            f"full output: artifact:{artifact}, {sections.count} sections; read one with "
            f'tool.call {{"name":"{READER}","arguments":{{"artifact":"{artifact}","section":1}}}}'
        ) in marker.text

        script.turns.extend(
            [
                _tool_turn(_read("find", {"artifact": artifact, "find": "results of operations"})),
                _tool_turn(
                    _read("second", {"artifact": f"artifact:{artifact}", "section": 2}),
                    _read("third", {"artifact": artifact, "section": 3}),
                    _read("fourth", {"artifact": artifact, "section": 4}),
                ),
                _tool_turn(
                    ScriptedToolCall(
                        name="sandbox.run_command", arguments={"command": ["ls"]}, call_id="ls"
                    )
                ),
                ScriptedTurn(text="Revenue rose in every reported period."),
            ]
        )
        second = await app.runs.submit("Read its results of operations.", session_id)
        [approval] = await asyncio.wait_for(_pending(app, second), 30)
        assert approval.tool_name == "sandbox.run_command"
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(second, app.principal)
        [command] = [i for i in invocations if i.tool_name == "sandbox.run_command"]
        assert command.policy_decision is not None
        assert command.policy_decision.untrusted_escalation is True
        await app.approvals.resolve(approval.id, ApprovalResolutionType.DENY)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(second), 30)
        events = await _events(app, session_id)
        requests = _requests(app)
        plan = await app.executor._context_planner.current(session_id)
        async with app.uow_factory() as uow:
            session = await uow.sessions.get(session_id, app.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)

    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    completed = [
        event.payload
        for event in events
        if event.event_type == "tool.call.completed" and event.payload["name"] == READER
    ]
    assert [payload["call_id"] for payload in completed] == ["find", "second", "third"]
    [refused] = [
        event.payload
        for event in events
        if event.event_type == "tool.call.failed" and event.payload["name"] == READER
    ]
    assert refused["call_id"] == "fourth"
    assert refused["reason_code"] == "tool.capture.step_limit"

    # The sections are recorded whole, and every read reaches the model in an
    # untrusted envelope. The fake model's 32k window truncates them later in
    # the request; the production window's budget holds two whole sections
    # (tests/unit/test_capture_admission_adr0261.py).
    recorded = {
        payload["call_id"]: ToolResultItem.model_validate(payload["result_item"])
        for payload in completed
    }
    for call_id, number in (("second", 2), ("third", 3)):
        assert recorded[call_id].trust is TrustLevel.EXTERNAL_UNTRUSTED
        assert recorded[call_id].content == [TextPart(text=sections.section_text(number))]
    assert "§1: " in str(recorded["find"].content)
    command_request = next(
        request
        for request in requests
        if any(
            isinstance(item, ToolResultItem) and item.call_id == "fourth"
            for item in request.conversation
        )
    )
    shown_results = {
        item.call_id: item
        for item in command_request.conversation
        if isinstance(item, ToolResultItem)
    }
    for call_id in ("find", "second", "third"):
        assert shown_results[call_id].trust is TrustLevel.EXTERNAL_UNTRUSTED

    # The reader is pinned behind tool.call and adds no prompt bytes.
    assert plan is not None
    assert READER in plan.deferred_tool_names
    assert READER not in plan.tool_names
    shown = build_prefix(
        agent,
        plan.tool_specs,
        plan.skill_catalog,
        plan.memory_snapshot,
        persona=plan.persona_text,
        deferred_tools=plan.deferred_tool_specs,
    )
    hidden = build_prefix(
        agent,
        plan.tool_specs,
        plan.skill_catalog,
        plan.memory_snapshot,
        persona=plan.persona_text,
        deferred_tools=[spec for spec in plan.deferred_tool_specs if spec.name != READER],
    )
    assert shown == hidden


async def _pending(app: Any, run_id: UUID) -> list[Any]:
    while True:
        pending = await app.approvals.list_pending(run_id=run_id)
        if pending:
            return list(pending)
        await asyncio.sleep(0.01)
