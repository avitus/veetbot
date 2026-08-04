"""One provider-neutral contract over every Milestone 3 adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from anthropic import APIConnectionError, APIResponseValidationError, APIStatusError
from openai import APIConnectionError as OpenAIAPIConnectionError
from pydantic import ValidationError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.models.recorded import (
    RecordedEventSource,
    RecordedFixture,
    RecordedModelProvider,
)
from agent_core.domain.messages import (
    CostSource,
    FakeModelScript,
    ModelAttempt,
    ModelCapabilities,
    ModelFailedEvent,
    ModelLimits,
    ModelPricing,
    ModelRequest,
    ModelTurn,
    ModelUsage,
    ReasoningSupport,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolCallDeltaEvent,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.model.cost import price_usage
from agent_core.model.streaming import ModelStreamAccumulator, collect_turn, validated_stream
from agent_core.ports.models import ModelProvider
from agent_core.tools.calculator import CalculatorTool
from tests.contract.model_fixtures import (
    ScriptedRawSource,
    anthropic_text_events,
    anthropic_tool_events,
    chat_text_events,
    chat_tool_events,
    openai_text_events,
    openai_tool_events,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-0000-0000-000000000301")
ATTEMPT = ModelAttempt(
    attempt_id=UUID("00000000-0000-0000-0000-000000000302"),
    run_id=RUN_ID,
    step_number=1,
    attempt_number=1,
    started_at=NOW,
)
ARGUMENTS = '{"expression":"17 * 23"}'


def request(conversation: list[Any] | None = None) -> ModelRequest:
    return ModelRequest(
        model_policy="contract",
        conversation=conversation or [UserMessage(content=[TextPart(text="calculate")])],
        tools=[CalculatorTool.spec],
        maximum_output_tokens=256,
    )


def resolved(provider: str) -> ResolvedModel:
    reasoning = (
        ReasoningSupport.IN_BAND if provider == "chat_completions" else ReasoningSupport.NATIVE
    )
    capabilities = ModelCapabilities(
        native_tool_calling=provider != "chat_completions",
        parallel_tool_calls=provider != "chat_completions",
        reasoning=reasoning,
    )
    return ResolvedModel(
        provider=provider,
        model="contract-model",
        capabilities=capabilities,
        resolved_at=NOW,
    )


def providers_for_tool(arguments: str = ARGUMENTS) -> list[tuple[str, ModelProvider]]:
    openai_source = ScriptedRawSource([openai_tool_events(arguments)])
    anthropic_source = ScriptedRawSource([anthropic_tool_events(arguments)])
    chat_source = ScriptedRawSource([chat_tool_events(arguments)])
    recorded_fixture = RecordedFixture(
        schema_version=1,
        provider_api="responses",
        events=openai_tool_events(arguments),
    )
    recorded = RecordedModelProvider(
        OpenAIResponsesProvider(event_source=RecordedEventSource(recorded_fixture))
    )
    fake = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="math.calculate",
                            arguments=arguments,
                            call_id="call-byte-1",
                        )
                    ]
                )
            ]
        ),
        FixedClock(NOW),
    )
    return [
        ("fake", fake),
        ("recorded", recorded),
        ("openai", OpenAIResponsesProvider(event_source=openai_source)),
        ("anthropic", AnthropicMessagesProvider(event_source=anthropic_source)),
        (
            "chat_completions",
            ChatCompletionsProvider(
                base_url="http://127.0.0.1:11434/v1",
                event_source=chat_source,
            ),
        ),
    ]


async def collect(provider: ModelProvider, provider_name: str) -> ModelTurn:
    try:
        return await collect_turn(provider.stream(request(), resolved(provider_name), ATTEMPT))
    finally:
        await provider.close()


def portable(turn: ModelTurn) -> dict[str, Any]:
    value = turn.model_dump(mode="json")
    value.pop("usage")
    value.pop("provider_metadata")
    value.pop("provider_reasoning_items")
    return value


async def test_contract_is_identical_across_fake_recorded_and_three_api_modes() -> None:
    turns = [await collect(provider, name) for name, provider in providers_for_tool()]
    assert all(portable(turn) == portable(turns[0]) for turn in turns[1:])
    assert turns[0].tool_calls[0].call_id == "call-byte-1"
    assert turns[0].stop_reason is StopReason.TOOL_USE


async def test_malformed_arguments_remain_a_recoverable_tool_turn_on_every_adapter() -> None:
    for name, provider in providers_for_tool('{"expression":'):
        turn = await collect(provider, name)
        assert turn.stop_reason is StopReason.TOOL_USE
        assert turn.tool_calls[0].parse_error == "arguments were not valid JSON"
        assert turn.tool_calls[0].raw_arguments == '{"expression":'


@pytest.mark.parametrize("provider_name", ["openai", "anthropic", "chat_completions"])
async def test_call_id_round_trips_through_provider_request(
    provider_name: str,
) -> None:
    source = ScriptedRawSource(
        [
            openai_text_events()
            if provider_name == "openai"
            else anthropic_text_events()
            if provider_name == "anthropic"
            else chat_text_events()
        ]
    )
    if provider_name == "openai":
        provider: ModelProvider = OpenAIResponsesProvider(event_source=source)
    elif provider_name == "anthropic":
        provider = AnthropicMessagesProvider(event_source=source)
    else:
        provider = ChatCompletionsProvider(
            base_url="http://127.0.0.1:11434/v1", event_source=source
        )
    history = [
        UserMessage(content=[TextPart(text="calculate")]),
        ToolCallItem(
            call_id="call-byte-1",
            item_index=0,
            name="math.calculate",
            arguments={"expression": "17 * 23"},
            raw_arguments=ARGUMENTS,
        ),
        ToolResultItem(call_id="call-byte-1", content=[TextPart(text="391")]),
    ]
    try:
        await collect_turn(provider.stream(request(history), resolved(provider_name), ATTEMPT))
    finally:
        await provider.close()
    rendered = source.requests[0]
    serialized = str(rendered)
    assert serialized.count("call-byte-1") >= 2


def test_usage_cost_is_exact_decimal_and_anthropic_reasoning_is_not_double_counted() -> None:
    usage = ModelUsage(
        input_tokens=1_000_000,
        cached_input_tokens=100_000,
        output_tokens=200_000,
        reasoning_tokens=None,
        provider="anthropic",
        model="contract-model",
    )
    pricing = ModelPricing(
        input_per_mtok=Decimal("5"),
        cached_input_per_mtok=Decimal("0.5"),
        output_per_mtok=Decimal("25"),
        reasoning_per_mtok=Decimal("30"),
        reasoning_priced_separately=True,
        source=CostSource.DOCS_SNAPSHOT,
    )
    assert price_usage(usage, pricing).cost == Decimal("9.55")
    assert price_usage(
        usage.model_copy(update={"reasoning_tokens": 100_000}), pricing
    ).cost == Decimal("10.05")


@pytest.mark.parametrize(
    "values",
    [
        {"context_window_tokens": 100, "max_output_tokens": 101},
        {
            "context_window_tokens": 100,
            "max_output_tokens": 50,
            "default_output_reserve": 51,
        },
    ],
)
def test_model_limits_reject_invalid_composed_values(values: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        ModelLimits.model_validate(values)


def test_empty_tool_arguments_close_as_an_empty_object() -> None:
    accumulator = ModelStreamAccumulator()
    accumulator.add(
        ToolCallDeltaEvent(
            attempt_id=ATTEMPT.attempt_id,
            run_id=RUN_ID,
            step_number=1,
            sequence=0,
            item_index=0,
            call_id="no-arguments",
            name="system.current_time",
            arguments_delta="",
        )
    )
    turn = accumulator.turn(usage=ModelUsage(), stop_reason=StopReason.TOOL_USE)
    assert turn.tool_calls[0].arguments == {}
    assert turn.tool_calls[0].parse_error is None


async def test_native_chat_completion_keeps_literal_xml_tool_text_visible() -> None:
    literal = '<tool_call>{"name":"documentation.only"}</tool_call>'
    source = ScriptedRawSource([chat_text_events(literal)])
    provider = ChatCompletionsProvider(
        base_url="http://127.0.0.1:11434/v1",
        event_source=source,
    )
    native = resolved("chat_completions").model_copy(
        update={
            "capabilities": resolved("chat_completions").capabilities.model_copy(
                update={"native_tool_calling": True}
            )
        }
    )
    try:
        turn = await collect_turn(provider.stream(request(), native, ATTEMPT))
    finally:
        await provider.close()
    assert turn.tool_calls == []
    content = turn.assistant_messages[0].content[0]
    assert isinstance(content, TextPart)
    assert content.text == literal


async def test_openai_encrypted_reasoning_round_trips_for_stateless_continuation() -> None:
    reasoning = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {"type": "reasoning", "id": "reasoning-item", "encrypted_content": "opaque"},
    }
    first_source = ScriptedRawSource([[reasoning, *openai_tool_events(ARGUMENTS)]])
    first = OpenAIResponsesProvider(event_source=first_source)
    first_turn = await collect_turn(first.stream(request(), resolved("openai"), ATTEMPT))
    await first.close()
    assert first_turn.provider_reasoning_items[0].provider_payload == {
        "id": "reasoning-item",
        "type": "reasoning",
        "encrypted_content": "opaque",
        "response_id": "resp-tool",
    }

    second_source = ScriptedRawSource([openai_text_events()])
    second = OpenAIResponsesProvider(event_source=second_source)
    continued = request(
        [
            UserMessage(content=[TextPart(text="calculate")]),
            *first_turn.provider_reasoning_items,
        ]
    )
    second_turn = await collect_turn(second.stream(continued, resolved("openai"), ATTEMPT))
    await second.close()
    assert "previous_response_id" not in second_source.requests[0]
    assert second_source.requests[0]["input"][-1] == {
        "id": "reasoning-item",
        "type": "reasoning",
        "encrypted_content": "opaque",
    }
    assert second_turn.provider_metadata is not None
    assert second_turn.provider_metadata.previous_response_id == "resp-tool"


async def test_openai_midstream_transport_failure_uses_the_next_sequence() -> None:
    async def disconnect(_request: dict[str, Any]) -> Any:
        yield {
            "type": "response.output_text.delta",
            "output_index": 0,
            "delta": "first",
        }
        yield {
            "type": "response.output_text.delta",
            "output_index": 0,
            "delta": "second",
        }
        raise OpenAIAPIConnectionError(
            request=httpx.Request("POST", "https://api.openai.com/v1/responses")
        )

    provider = OpenAIResponsesProvider(event_source=disconnect)
    events = []
    try:
        async for event in validated_stream(
            provider.stream(request(), resolved("openai"), ATTEMPT)
        ):
            events.append(event)
    finally:
        await provider.close()
    assert [event.sequence for event in events] == [0, 1, 2]
    assert isinstance(events[-1], ModelFailedEvent)
    assert events[-1].error.kind == "transient"


def test_provider_usage_counters_are_clamped_to_their_totals() -> None:
    openai_usage = OpenAIResponsesProvider._usage(
        {
            "usage": {
                "input_tokens": -1,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens": 3,
                "output_tokens_details": {"reasoning_tokens": 10},
            }
        },
        resolved("openai"),
    )
    assert openai_usage.input_tokens == openai_usage.cached_input_tokens == 0
    assert openai_usage.output_tokens == 3
    assert openai_usage.reasoning_tokens == 3

    chat_usage = ChatCompletionsProvider._usage(
        {
            "prompt_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens": -1,
        },
        resolved("chat_completions"),
    )
    assert chat_usage.input_tokens == chat_usage.cached_input_tokens == 2
    assert chat_usage.output_tokens == 0


async def test_recorded_fixture_fails_after_its_declared_streams_are_exhausted() -> None:
    fixture = RecordedFixture(
        schema_version=1,
        provider_api="responses",
        streams=[openai_text_events("once")],
    )
    provider = RecordedModelProvider(
        OpenAIResponsesProvider(event_source=RecordedEventSource(fixture))
    )
    try:
        first = await collect_turn(provider.stream(request(), resolved("openai"), ATTEMPT))
        assert first.assistant_messages[0].content == [TextPart(text="once")]
        with pytest.raises(ValueError, match="streams are exhausted"):
            await collect_turn(provider.stream(request(), resolved("openai"), ATTEMPT))
    finally:
        await provider.close()


async def test_midstream_chat_transport_failure_keeps_a_gapless_terminal_sequence() -> None:
    async def disconnect(_request: dict[str, Any]) -> Any:
        yield {
            "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
        }
        raise httpx.ReadError(
            "synthetic disconnect",
            request=httpx.Request("POST", "http://127.0.0.1/chat/completions"),
        )

    provider = ChatCompletionsProvider(
        base_url="http://127.0.0.1:11434/v1",
        event_source=disconnect,
    )
    events = []
    try:
        async for event in validated_stream(
            provider.stream(request(), resolved("chat_completions"), ATTEMPT)
        ):
            events.append(event)
    finally:
        await provider.close()
    assert [event.sequence for event in events] == [0, 1]
    assert isinstance(events[-1], ModelFailedEvent)
    assert events[-1].error.kind == "transient"


async def test_anthropic_usage_and_indexes_default_defensively() -> None:
    events = anthropic_text_events("safe")
    events[0]["message"]["usage"] = {
        "input_tokens": "malformed",
        "output_tokens": 5,
    }
    events[2]["index"] = "malformed"
    events[4].pop("usage")
    source = ScriptedRawSource([events])
    provider = AnthropicMessagesProvider(event_source=source)
    try:
        turn = await collect_turn(provider.stream(request(), resolved("anthropic"), ATTEMPT))
    finally:
        await provider.close()
    assert turn.usage.input_tokens == 0
    assert turn.usage.output_tokens == 5
    assert turn.assistant_messages[0].item_index == 0


async def test_anthropic_error_type_drives_retry_and_sdk_errors_are_normalized() -> None:
    calls = 0

    async def overload_then_text(_request: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            request_value = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise APIStatusError(
                "overloaded",
                response=httpx.Response(400, request=request_value),
                body={"error": {"type": "overloaded_error"}},
            )
        for event in anthropic_text_events("retried"):
            yield event

    provider = AnthropicMessagesProvider(event_source=overload_then_text)
    try:
        turn = await collect_turn(provider.stream(request(), resolved("anthropic"), ATTEMPT))
    finally:
        await provider.close()
    assert turn.assistant_messages[0].content == [TextPart(text="retried")]
    assert calls == 2

    async def invalid_response(_request: dict[str, Any]) -> Any:
        request_value = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(200, request=request_value)
        raise APIResponseValidationError(response, {"bad": "body"})
        yield {}

    invalid = AnthropicMessagesProvider(event_source=invalid_response)
    normalized = []
    try:
        async for event in validated_stream(
            invalid.stream(request(), resolved("anthropic"), ATTEMPT)
        ):
            normalized.append(event)
    finally:
        await invalid.close()
    assert len(normalized) == 1
    assert isinstance(normalized[0], ModelFailedEvent)
    assert normalized[0].error.kind == "permanent"


async def test_anthropic_midstream_transport_failure_keeps_sequence_gapless() -> None:
    async def disconnect(_request: dict[str, Any]) -> Any:
        for event in anthropic_text_events("partial")[:3]:
            yield event
        raise APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    provider = AnthropicMessagesProvider(event_source=disconnect)
    events = []
    try:
        async for event in validated_stream(
            provider.stream(request(), resolved("anthropic"), ATTEMPT)
        ):
            events.append(event)
    finally:
        await provider.close()
    assert [event.sequence for event in events] == [0, 1, 2]
    assert isinstance(events[-1], ModelFailedEvent)
