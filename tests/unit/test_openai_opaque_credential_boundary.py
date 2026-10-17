"""ADR-0210 admits only embedded OpenAI opaque collisions, never plain secrets."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from agent_core.domain.messages import (
    AssistantMessage,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelPermanentError,
    ModelTurn,
    ProviderMetadata,
    ProviderReasoningItem,
    TextPart,
    ToolCallItem,
)
from agent_core.model.streaming import ModelStreamError, validated_stream
from tests.gates.test_model_m3 import ATTEMPT_ID, completed

COLLISION = "gAAAA-ciphertext-sk-" + "synthetic123456=="
KEY = "sk-" + "synthetic123456"


def terminal(
    value: object = COLLISION, *, failed: bool = False
) -> ModelCompletedEvent | ModelFailedEvent:
    event = completed(0)
    event.turn.usage.provider = "openai"
    event.turn.provider_metadata = ProviderMetadata(provider_api="responses")
    event.turn.provider_reasoning_items = [
        ProviderReasoningItem(
            item_index=0,
            provider="openai",
            provider_payload={"type": "reasoning", "encrypted_content": value},
        )
    ]
    if not failed:
        return event
    return ModelFailedEvent(
        **event.model_dump(include={"attempt_id", "run_id", "step_number", "sequence"}),
        error=ModelPermanentError(
            provider="openai", model="test", attempt_id=ATTEMPT_ID, message="safe failure"
        ),
        partial_turn=event.turn,
    )


def turn_of(event: ModelCompletedEvent | ModelFailedEvent) -> ModelTurn:
    turn = event.turn if isinstance(event, ModelCompletedEvent) else event.partial_turn
    assert turn is not None
    return turn


async def events(event: ModelEvent) -> AsyncIterator[ModelEvent]:
    yield event


@pytest.mark.parametrize("failed", [False, True])
async def test_embedded_openai_collision_preserves_original_event_and_usage(failed: bool) -> None:
    event = terminal(failed=failed)
    original = event.model_dump_json()
    received = [item async for item in validated_stream(events(event))]
    assert received == [event]
    assert received[0] is event
    assert event.model_dump_json() == original


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize(
    "value",
    [
        KEY,
        "  " + KEY + "  ",
        KEY + "==",
        "sk-ant-" + "synthetic123456",
        "sk-proj-" + "synthetic123456",
        "prefix Bearer " + "synthetic123456",
        "prefix Authorization=" + "synthetic123456",
        "prefix -----BEGIN " + "PRIVATE KEY-----",
        COLLISION + " Authorization=" + "synthetic123456",
        "quoted '" + KEY + "'",
        {"nested": COLLISION},
        [COLLISION],
    ],
)
async def test_standalone_credentials_other_detectors_and_nonopaque_values_still_fail(
    value: object, failed: bool
) -> None:
    event = terminal(value, failed=failed)
    with pytest.raises(ModelStreamError, match="credential-shaped") as caught:
        _ = [item async for item in validated_stream(events(event))]
    assert COLLISION not in json.dumps(caught.value.diagnostic_details)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize(
    "mismatch", ["provider", "usage", "metadata", "api", "type", "field", "nested", "key"]
)
async def test_only_the_agreed_openai_direct_value_is_admitted(mismatch: str, failed: bool) -> None:
    event = terminal(failed=failed)
    turn = turn_of(event)
    item = turn.provider_reasoning_items[0]
    if mismatch == "provider":
        item.provider = "anthropic"
    elif mismatch == "usage":
        turn.usage.provider = "anthropic"
    elif mismatch == "metadata":
        turn.provider_metadata = None
    elif mismatch == "api":
        turn.provider_metadata = ProviderMetadata(provider_api="chat_completions")
    elif mismatch == "type":
        item.provider_payload["type"] = "unknown"
    elif mismatch == "field":
        item.provider_payload["unknown"] = item.provider_payload.pop("encrypted_content")
    elif mismatch == "nested":
        item.provider_payload["encrypted_content"] = {"encrypted_content": COLLISION}
    else:
        item.provider_payload[KEY] = "safe"
    with pytest.raises(ModelStreamError, match="credential-shaped"):
        _ = [row async for row in validated_stream(events(event))]


async def test_partial_turn_cannot_borrow_openai_authority_from_a_different_error() -> None:
    event = terminal(failed=True)
    assert isinstance(event, ModelFailedEvent)
    event.error.provider = "anthropic"
    with pytest.raises(ModelStreamError, match="credential-shaped"):
        _ = [row async for row in validated_stream(events(event))]


@pytest.mark.parametrize("failed", [False, True])
async def test_admitted_collision_does_not_mask_a_later_rejection_or_its_field(
    failed: bool,
) -> None:
    event = terminal(failed=failed)
    turn_of(event).provider_reasoning_items[0].provider_payload["other"] = KEY
    with pytest.raises(ModelStreamError, match="credential-shaped") as caught:
        _ = [row async for row in validated_stream(events(event))]
    prefix = "partial_turn" if failed else "turn"
    assert caught.value.diagnostic_details["protocol_field_path"] == (
        prefix + ".provider_reasoning_items.[].provider_payload.<value>"
    )


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("surface", ["text", "tool", "summary", "metadata", "anthropic"])
async def test_an_admitted_value_never_exempts_other_turn_content(
    surface: str, failed: bool
) -> None:
    event = terminal(failed=failed)
    turn = turn_of(event)
    if surface == "text":
        turn.assistant_messages = [AssistantMessage(content=[TextPart(text=KEY)])]
    elif surface == "tool":
        turn.tool_calls = [
            ToolCallItem(
                call_id="call",
                item_index=1,
                name="math.calculate",
                arguments={"value": KEY},
                raw_arguments=json.dumps({"value": KEY}),
            )
        ]
    elif surface == "summary":
        turn.provider_reasoning_items[0].provider_payload["summary"] = [{"text": KEY}]
    elif surface == "metadata":
        turn.provider_metadata = ProviderMetadata(provider_api="responses", response_id=KEY)
    else:
        turn.provider_reasoning_items.append(
            ProviderReasoningItem(
                item_index=1,
                provider="anthropic",
                provider_payload={"type": "thinking", "signature": COLLISION},
            )
        )
    with pytest.raises(ModelStreamError, match="credential-shaped"):
        _ = [row async for row in validated_stream(events(event))]


async def test_an_admitted_partial_value_never_exempts_its_error_message() -> None:
    event = terminal(failed=True)
    assert isinstance(event, ModelFailedEvent)
    event.error.message = KEY
    with pytest.raises(ModelStreamError, match="credential-shaped") as caught:
        _ = [row async for row in validated_stream(events(event))]
    assert caught.value.diagnostic_details["protocol_field_path"] == "error.message"
