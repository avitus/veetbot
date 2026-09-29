"""Denied tool calls project into session history (ADR-0142)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.events import (
    TOOL_CALL_DENIED_PAYLOAD_VERSION,
    EventEnvelope,
    conversation_items,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolResultItem,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import TextContentBlock
from tests.unit.test_config import base_environment

LEGACY_DENIAL = {
    "name": "demo.external_write",
    "call_id": "call-1",
    "reason_code": "approval.denied",
}


def _event(event_type: str, payload: dict[str, Any], *, version: int = 1) -> EventEnvelope:
    return EventEnvelope(
        id=7,
        session_id=UUID(int=1),
        run_id=None,
        sequence=5,
        event_type=event_type,
        payload_schema_version=version,
        actor_type="runtime",
        actor_id=None,
        payload=payload,
        trace_id=None,
        created_at=datetime(2026, 9, 29, tzinfo=UTC),
    )


def test_an_upcast_legacy_denial_projects_only_what_it_recorded() -> None:
    (item,) = conversation_items(
        _event("tool.call.denied", {**LEGACY_DENIAL, "result_item": None}, version=2)
    )

    assert isinstance(item, ToolResultItem)
    assert item.call_id == "call-1"
    assert item.is_error
    assert item.trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert item.source_event_sequence == 5
    assert len(item.content) == 1
    assert isinstance(item.content[0], TextPart)
    assert json.loads(item.content[0].text) == {
        "status": "denied",
        "action": "demo.external_write",
        "reason_code": "approval.denied",
    }


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        # A payload that never passed through the upcaster is not a legacy denial.
        ("tool.call.denied", LEGACY_DENIAL),
        ("tool.call.denied", {**LEGACY_DENIAL, "result_item": "not an item"}),
        ("tool.call.denied", {**LEGACY_DENIAL, "reason_code": "", "result_item": None}),
        ("tool.call.denied", {"name": "demo.external_write", "result_item": None}),
        ("tool.call.completed", {**LEGACY_DENIAL, "result_item": None}),
        ("tool.call.failed", {**LEGACY_DENIAL, "result_item": None}),
        ("tool.call.uncertain", {**LEGACY_DENIAL, "result_item": None}),
    ],
)
def test_a_tool_result_event_without_a_projectable_item_is_rejected(
    event_type: str, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="has no result item"):
        conversation_items(_event(event_type, payload, version=2))


def test_a_recorded_denial_item_projects_unchanged() -> None:
    recorded = ToolResultItem(
        call_id="call-1",
        content=[TextPart(text='{"status":"denied"}')],
        is_error=True,
        trust=TrustLevel.INTERNAL_TOOL,
    )
    (item,) = conversation_items(
        _event(
            "tool.call.denied",
            {**LEGACY_DENIAL, "result_item": recorded.model_dump(mode="json")},
            version=TOOL_CALL_DENIED_PAYLOAD_VERSION,
        )
    )

    assert item == recorded.model_copy(update={"source_event_sequence": 5})


async def test_a_denied_approval_leaves_the_session_open_to_the_next_message() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "never written"},
                        call_id="denied-in-memory",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Not written.", stop_reason=StopReason.END_TURN),
            ScriptedTurn(text="Second answer.", stop_reason=StopReason.END_TURN),
        ]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(settings=settings, script=script, sequential_ids=True) as composition:
        principal = composition.principal
        session = await composition.services.sessions.create(principal, "general", {})
        first = await composition.services.runs.submit(
            principal, session.id, [TextContentBlock(text="record a write")], None, None
        )
        (approval,) = await composition.approvals.list_pending(run_id=first.run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.DENY)
        denied_run = await composition.runs.wait_terminal(first.run_id)
        second = await composition.services.runs.submit(
            principal, session.id, [TextContentBlock(text="then answer this")], None, None
        )
        continued = await composition.runs.wait_terminal(second.run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session.id, 0, principal)

    assert denied_run.status is RunStatus.COMPLETED
    assert continued.status is RunStatus.COMPLETED
    assert continued.final_message == "Second answer."
    (denied,) = [event for event in events if event.event_type == "tool.call.denied"]
    assert denied.payload_schema_version == TOOL_CALL_DENIED_PAYLOAD_VERSION
    assert denied.payload["reason_code"] == "approval.denied"
    assert ToolResultItem.model_validate(denied.payload["result_item"]).call_id == (
        "denied-in-memory"
    )
