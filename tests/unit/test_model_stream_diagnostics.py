"""Credential refusals identify their surface without recording its contents."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from agent_core.domain.messages import (
    ModelEvent,
    ModelFailedEvent,
    ModelPermanentError,
    ProviderReasoningItem,
    ReasoningDeltaEvent,
)
from agent_core.model.streaming import ModelStreamError, validated_stream
from tests.gates.test_model_m3 import ATTEMPT_ID, completed, text, tool


async def rejected(event: ModelEvent) -> ModelStreamError:
    async def source() -> AsyncIterator[ModelEvent]:
        yield event

    with pytest.raises(ModelStreamError, match="credential-shaped") as caught:
        async for _ in validated_stream(source()):
            pytest.fail("A rejected event escaped the validator")
    return caught.value


@pytest.mark.parametrize(
    ("value", "detector"),
    [
        ("sk-" + "synthetic123456", "provider_key"),
        ("Bearer " + "synthetic123456", "bearer"),
        ("Authorization=" + "synthetic123456", "authorization"),
        ("-----BEGIN " + "PRIVATE KEY-----", "private_key"),
    ],
)
async def test_credential_failure_names_only_fixed_diagnostic_labels(
    value: str,
    detector: str,
) -> None:
    error = await rejected(text(0, 0, value))
    assert getattr(error, "diagnostic_details", {}) == {
        "protocol_event_kind": "text_delta",
        "protocol_field_path": "text",
        "protocol_detector": detector,
    }
    assert value not in str(error)
    assert value not in json.dumps(error.diagnostic_details)


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize(
    ("provider", "payload_type", "field", "expected_field"),
    [
        ("openai", "reasoning", "encrypted_content", "encrypted_content"),
        ("anthropic", "thinking", "signature", "signature"),
        ("anthropic", "redacted_thinking", "data", "data"),
        ("unknown", "reasoning", "encrypted_content", "<value>"),
        ("openai", "unknown", "encrypted_content", "<value>"),
        ("openai", "reasoning", "private-field-name", "<value>"),
    ],
)
async def test_opaque_payload_is_still_rejected_with_a_safe_field_label(
    failed: bool,
    provider: str,
    payload_type: str,
    field: str,
    expected_field: str,
) -> None:
    terminal = completed(0)
    value = "ciphertext-prefix-sk-" + "synthetic123456"
    terminal.turn.provider_reasoning_items.append(
        ProviderReasoningItem(
            item_index=0,
            provider=provider,
            provider_payload={"type": payload_type, field: value},
        )
    )
    event: ModelEvent = terminal
    prefix = "turn"
    if failed:
        event = ModelFailedEvent(
            **terminal.model_dump(include={"attempt_id", "run_id", "step_number", "sequence"}),
            error=ModelPermanentError(
                provider="openai",
                model="test",
                attempt_id=ATTEMPT_ID,
                message="safe failure",
            ),
            partial_turn=terminal.turn,
        )
        prefix = "partial_turn"
    error = await rejected(event)
    assert getattr(error, "diagnostic_details", {}) == {
        "protocol_event_kind": "failed" if failed else "completed",
        "protocol_field_path": (
            f"{prefix}.provider_reasoning_items.[].provider_payload.{expected_field}"
        ),
        "protocol_detector": "provider_key",
    }
    assert value not in json.dumps(error.diagnostic_details)
    assert "private-field-name" not in json.dumps(error.diagnostic_details)


@pytest.mark.parametrize("in_key", [False, True])
async def test_arbitrary_mapping_keys_and_nested_values_never_enter_diagnostics(
    in_key: bool,
) -> None:
    event = completed(0)
    value = "sk-" + "synthetic123456"
    event.turn.provider_reasoning_items.append(
        ProviderReasoningItem(
            item_index=0,
            provider="openai",
            provider_payload={value: "safe"}
            if in_key
            else {"private-field-name": {"secret-name": value}},
        )
    )
    error = await rejected(event)
    suffix = "<key>" if in_key else "<value>.<value>"
    assert getattr(error, "diagnostic_details", {}).get("protocol_field_path") == (
        "turn.provider_reasoning_items.[].provider_payload." + suffix
    )
    diagnostic = json.dumps(error.diagnostic_details)
    assert all(part not in diagnostic for part in [value, "private-field-name", "secret-name"])


async def test_tool_argument_diagnostic_does_not_parse_or_publish_argument_keys() -> None:
    event = tool(0, 0).model_copy(
        update={"arguments_delta": '{"private-name":"sk-' + 'synthetic123456"}'}
    )
    error = await rejected(event)
    assert getattr(error, "diagnostic_details", {}) == {
        "protocol_event_kind": "tool_call_delta",
        "protocol_field_path": "arguments_delta",
        "protocol_detector": "provider_key",
    }


@pytest.mark.parametrize(
    "field", ["protocol_event_kind", "protocol_field_path", "protocol_detector"]
)
def test_malformed_diagnostic_labels_are_dropped_before_persistence(field: str) -> None:
    details = {
        "protocol_event_kind": "completed",
        "protocol_field_path": (
            "turn.provider_reasoning_items.[].provider_payload.encrypted_content"
        ),
        "protocol_detector": "provider_key",
    }
    details[field] = "sk-" + "synthetic123456"
    error = ModelStreamError("safe refusal", diagnostic_details=details)
    assert error.diagnostic_details == {}


async def test_reasoning_display_remains_scanned_without_retaining_reasoning() -> None:
    event = ReasoningDeltaEvent(
        **text(0, 0, "sk-" + "synthetic123456").model_dump(exclude={"kind"}), is_summary=True
    )
    error = await rejected(event)
    assert error.diagnostic_details == {
        "protocol_event_kind": "reasoning_delta",
        "protocol_field_path": "text",
        "protocol_detector": "provider_key",
    }


async def test_failed_event_error_is_still_scanned() -> None:
    event = ModelFailedEvent(
        **completed(0).model_dump(include={"attempt_id", "run_id", "step_number", "sequence"}),
        error=ModelPermanentError(
            provider="openai",
            model="test",
            attempt_id=ATTEMPT_ID,
            message="sk-" + "synthetic123456",
        ),
    )
    error = await rejected(event)
    assert error.diagnostic_details == {
        "protocol_event_kind": "failed",
        "protocol_field_path": "error.message",
        "protocol_detector": "provider_key",
    }
