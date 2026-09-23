"""The fake provider gives an opaque reasoning item an index of its own.

A real provider numbers its reasoning output item before the text or tool
items that follow. The fake must do the same even when the scripted turn has
no reasoning summary to stream, or two items in one turn share an index.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelCompletedEvent,
    ModelLimits,
    ModelPricing,
    ModelRequest,
    ResolvedModel,
    ScriptedTurn,
    TextDeltaEvent,
    ToolCallDeltaEvent,
)
from tests.contract.support import NOW


async def test_an_opaque_reasoning_item_never_shares_an_index_with_text_or_tools() -> None:
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text="answer",
                    provider_reasoning_payload={"id": "rs_opaque", "summary": []},
                )
            ]
        ),
        FixedClock(NOW),
    )
    events = [
        event
        async for event in provider.stream(
            ModelRequest(model_policy="test", conversation=[], tools=[], maximum_output_tokens=50),
            ResolvedModel(
                provider="fake",
                model="scripted",
                resolved_at=NOW,
                limits=ModelLimits(
                    context_window_tokens=1000, max_output_tokens=50, default_output_reserve=10
                ),
                pricing=ModelPricing(input_per_mtok=Decimal(1), output_per_mtok=Decimal(1)),
            ),
            ModelAttempt(
                attempt_id=uuid4(), run_id=uuid4(), step_number=1, attempt_number=1, started_at=NOW
            ),
        )
    ]
    completed = next(event for event in events if isinstance(event, ModelCompletedEvent))
    [reasoning_item] = completed.turn.provider_reasoning_items
    content_indexes = {
        event.item_index
        for event in events
        if isinstance(event, (TextDeltaEvent, ToolCallDeltaEvent))
    }
    assert content_indexes, "the turn streamed its text"
    assert reasoning_item.item_index not in content_indexes
