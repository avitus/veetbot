"""Anthropic Messages adapter; the only module that imports the Anthropic SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from anthropic import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncAnthropic

from agent_core.adapters.models.common import (
    RawEventSource,
    as_mapping,
    failed_event,
    nested,
    text_content,
    tool_definition,
)
from agent_core.adapters.models.registry import ANTHROPIC_CAPABILITY_CEILING
from agent_core.domain.messages import (
    AssistantMessage,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ModelUsage,
    ProviderMetadata,
    ProviderReasoningItem,
    ReasoningDeltaEvent,
    ResolvedModel,
    StopReason,
    SystemMessage,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallItem,
    ToolResultItem,
    UsageEvent,
    UserMessage,
)
from agent_core.model.cost import price_usage
from agent_core.model.streaming import ModelStreamAccumulator, ModelStreamError

TRANSIENT_ERROR_TYPES = frozenset({"overloaded_error", "rate_limit_error", "api_error"})


class AnthropicMessagesProvider:
    """Translate official Messages stream events into the neutral contract."""

    name = "anthropic"
    CAPABILITY_CEILING = ANTHROPIC_CAPABILITY_CEILING

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        event_source: RawEventSource | None = None,
        client: object | None = None,
        max_internal_attempts: int = 3,
    ) -> None:
        self._event_source = event_source
        self._client = client
        self._owns_client = client is None and event_source is None
        self._max_internal_attempts = max_internal_attempts
        if self._owns_client:
            if not api_key:
                raise ValueError("Anthropic provider requires its resolved credential")
            self._client = AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)

    async def _sdk_events(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError("Anthropic client is unavailable")
        stream = await cast(Any, self._client).messages.create(**payload, stream=True)
        async for event in stream:
            yield as_mapping(event)

    def _source(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if self._event_source is not None:
            return self._event_source(payload)
        return self._sdk_events(payload)

    async def stream(
        self,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        try:
            payload, sent, dropped = self._request_payload(request, resolved)
        except (TypeError, ValueError, ModelStreamError):
            yield failed_event(
                attempt=attempt,
                provider=self.name,
                model=resolved.model,
                sequence=0,
                category="protocol",
                detail="invalid neutral request history",
            )
            return
        for internal_attempt in range(1, self._max_internal_attempts + 1):
            emitted_count = 0
            try:
                async for event in self._translate(
                    self._source(payload), resolved, attempt, sent=sent, dropped=dropped
                ):
                    emitted_count = event.sequence + 1
                    yield event
                return
            except (APIConnectionError, APITimeoutError):
                if emitted_count == 0 and internal_attempt < self._max_internal_attempts:
                    continue
                yield failed_event(
                    attempt=attempt,
                    provider=self.name,
                    model=resolved.model,
                    sequence=emitted_count,
                    category="transient",
                    provider_code="transport_error",
                    stream_had_output=emitted_count > 0,
                )
                return
            except APIStatusError as exc:
                error_type = _status_error_type(exc)
                transient = (
                    exc.status_code == 429
                    or exc.status_code >= 500
                    or error_type in TRANSIENT_ERROR_TYPES
                )
                if (
                    transient
                    and emitted_count == 0
                    and internal_attempt < self._max_internal_attempts
                ):
                    continue
                yield failed_event(
                    attempt=attempt,
                    provider=self.name,
                    model=resolved.model,
                    sequence=emitted_count,
                    category="transient" if transient else "permanent",
                    provider_code=f"http_{exc.status_code}",
                    http_status=exc.status_code,
                    stream_had_output=emitted_count > 0,
                )
                return
            except APIError:
                yield failed_event(
                    attempt=attempt,
                    provider=self.name,
                    model=resolved.model,
                    sequence=emitted_count,
                    category="permanent",
                    provider_code="sdk_error",
                    stream_had_output=emitted_count > 0,
                )
                return

    async def _translate(
        self,
        source: AsyncIterator[dict[str, Any]],
        resolved: ResolvedModel,
        attempt: ModelAttempt,
        *,
        sent: int,
        dropped: int,
    ) -> AsyncIterator[ModelEvent]:
        sequence = 0
        accumulator = ModelStreamAccumulator()
        block_types: dict[int, str] = {}
        tool_identity: dict[int, tuple[str, str]] = {}
        thinking_text: dict[int, str] = {}
        thinking_signature: dict[int, str] = {}
        reasoning_items: list[ProviderReasoningItem] = []
        input_tokens = 0
        cached_tokens = 0
        cache_write_tokens = 0
        output_tokens = 0
        stop_reason = StopReason.END_TURN
        response_id: str | None = None
        request_id: str | None = None
        response_model: str | None = None
        async for raw in source:
            event_type = raw.get("type")
            if event_type == "message_start":
                message = raw.get("message")
                if not isinstance(message, dict):
                    message = {}
                response_id = _optional_string(message.get("id"))
                request_id = _optional_string(raw.get("request_id"))
                response_model = _optional_string(message.get("model"))
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
                input_tokens = _integer(usage.get("input_tokens"))
                cached_tokens = _integer(usage.get("cache_read_input_tokens"))
                cache_write_tokens = _integer(usage.get("cache_creation_input_tokens"))
                output_tokens = _integer(usage.get("output_tokens"))
                provisional = self._usage(
                    resolved,
                    input_tokens=input_tokens,
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens,
                    output_tokens=output_tokens,
                )
                yield UsageEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=sequence,
                    usage=provisional,
                )
                sequence += 1
            elif event_type == "content_block_start":
                index = _integer(raw.get("index"))
                block = raw.get("content_block")
                if not isinstance(block, dict):
                    block = {}
                block_type = str(block.get("type", ""))
                block_types[index] = block_type
                if block_type == "tool_use":
                    call_id = str(block.get("id", ""))
                    name = str(block.get("name", ""))
                    tool_identity[index] = (call_id, name)
                    tool_event = ToolCallDeltaEvent(
                        attempt_id=attempt.attempt_id,
                        run_id=attempt.run_id,
                        step_number=attempt.step_number,
                        sequence=sequence,
                        item_index=index,
                        call_id=call_id,
                        name=name,
                        arguments_delta="",
                    )
                    accumulator.add(tool_event)
                    yield tool_event
                    sequence += 1
                elif block_type == "thinking":
                    thinking_text[index] = str(block.get("thinking", ""))
                    thinking_signature[index] = str(block.get("signature", ""))
                elif block_type == "redacted_thinking":
                    reasoning_items.append(
                        ProviderReasoningItem(
                            item_index=index,
                            provider=self.name,
                            provider_payload=block,
                        )
                    )
                elif block_type == "server_tool_use":
                    yield failed_event(
                        attempt=attempt,
                        provider=self.name,
                        model=resolved.model,
                        sequence=sequence,
                        category="protocol",
                        detail="unexpected server_tool_use block",
                    )
                    return
            elif event_type == "content_block_delta":
                index = _integer(raw.get("index"))
                delta = raw.get("delta")
                if not isinstance(delta, dict):
                    delta = {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text_event = TextDeltaEvent(
                        attempt_id=attempt.attempt_id,
                        run_id=attempt.run_id,
                        step_number=attempt.step_number,
                        sequence=sequence,
                        item_index=index,
                        text=str(delta.get("text", "")),
                    )
                    accumulator.add(text_event)
                    yield text_event
                    sequence += 1
                elif delta_type == "input_json_delta":
                    identity = tool_identity.get(index)
                    if identity is None:
                        yield failed_event(
                            attempt=attempt,
                            provider=self.name,
                            model=resolved.model,
                            sequence=sequence,
                            category="protocol",
                            detail="tool input arrived before tool_use",
                        )
                        return
                    tool_event = ToolCallDeltaEvent(
                        attempt_id=attempt.attempt_id,
                        run_id=attempt.run_id,
                        step_number=attempt.step_number,
                        sequence=sequence,
                        item_index=index,
                        call_id=identity[0],
                        name=identity[1],
                        arguments_delta=str(delta.get("partial_json", "")),
                    )
                    accumulator.add(tool_event)
                    yield tool_event
                    sequence += 1
                elif delta_type == "thinking_delta":
                    text = str(delta.get("thinking", ""))
                    thinking_text[index] = thinking_text.get(index, "") + text
                    yield ReasoningDeltaEvent(
                        attempt_id=attempt.attempt_id,
                        run_id=attempt.run_id,
                        step_number=attempt.step_number,
                        sequence=sequence,
                        item_index=index,
                        text=text,
                        is_summary=False,
                    )
                    sequence += 1
                elif delta_type == "signature_delta":
                    thinking_signature[index] = thinking_signature.get(index, "") + str(
                        delta.get("signature", "")
                    )
            elif event_type == "content_block_stop":
                index = _integer(raw.get("index"))
                if block_types.get(index) == "thinking":
                    reasoning_items.append(
                        ProviderReasoningItem(
                            item_index=index,
                            provider=self.name,
                            provider_payload={
                                "type": "thinking",
                                "thinking": thinking_text.get(index, ""),
                                "signature": thinking_signature.get(index, ""),
                            },
                        )
                    )
            elif event_type == "message_delta":
                delta = raw.get("delta")
                if not isinstance(delta, dict):
                    delta = {}
                stop_reason = _stop_reason(delta.get("stop_reason"))
                usage = raw.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
                output_tokens = _integer(usage.get("output_tokens"), default=output_tokens)
                provisional = self._usage(
                    resolved,
                    input_tokens=input_tokens,
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens,
                    output_tokens=output_tokens,
                )
                yield UsageEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=sequence,
                    usage=provisional,
                )
                sequence += 1
            elif event_type == "message_stop":
                usage = self._usage(
                    resolved,
                    input_tokens=input_tokens,
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens,
                    output_tokens=output_tokens,
                )
                metadata = ProviderMetadata(
                    provider_api="messages",
                    response_id=response_id,
                    request_id=request_id,
                    resolved_model=response_model,
                    cache_breakpoints_sent=sent,
                    cache_breakpoints_dropped=dropped,
                )
                turn = accumulator.turn(
                    usage=usage,
                    stop_reason=stop_reason,
                    metadata=metadata,
                    reasoning_items=reasoning_items,
                )
                yield ModelCompletedEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=sequence,
                    turn=turn,
                    stop_reason=stop_reason,
                    stop_sequence=None,
                )
                return
            elif event_type == "error":
                code = nested(raw, "error", "type")
                transient = code in TRANSIENT_ERROR_TYPES
                yield failed_event(
                    attempt=attempt,
                    provider=self.name,
                    model=resolved.model,
                    sequence=sequence,
                    category="transient" if transient else "permanent",
                    provider_code=None if code is None else str(code),
                    stream_had_output=sequence > 0,
                )
                return
        yield failed_event(
            attempt=attempt,
            provider=self.name,
            model=resolved.model,
            sequence=sequence,
            category="protocol",
            detail="Messages stream ended without message_stop",
        )

    @staticmethod
    def _usage(
        resolved: ResolvedModel,
        *,
        input_tokens: int,
        cached_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
    ) -> ModelUsage:
        normalized = ModelUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            cache_write_input_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=None,
            provider="anthropic",
            model=resolved.model,
        )
        return price_usage(normalized, resolved.pricing)

    @staticmethod
    def _request_payload(
        request: ModelRequest, resolved: ResolvedModel
    ) -> tuple[dict[str, Any], int, int]:
        from agent_core.model.streaming import validate_conversation_pairing

        validate_conversation_pairing(request.conversation)
        hints = [] if request.cache_hints is None else request.cache_hints.breakpoints
        sent = min(len(hints), resolved.limits.max_cache_breakpoints)
        dropped = len(hints) - sent
        system: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        tools = [tool_definition(tool, anthropic=True) for tool in request.tools]
        for item in request.conversation:
            if isinstance(item, SystemMessage):
                system.append({"type": "text", "text": text_content(item.content)})
            elif isinstance(item, UserMessage):
                _append_message(
                    messages,
                    "user",
                    {"type": "text", "text": text_content(item.content)},
                )
            elif isinstance(item, AssistantMessage):
                _append_message(
                    messages,
                    "assistant",
                    {"type": "text", "text": text_content(item.content)},
                )
            elif isinstance(item, ToolCallItem):
                _append_message(
                    messages,
                    "assistant",
                    {
                        "type": "tool_use",
                        "id": item.call_id,
                        "name": item.name,
                        "input": item.arguments,
                    },
                )
            elif isinstance(item, ToolResultItem):
                _append_message(
                    messages,
                    "user",
                    {
                        "type": "tool_result",
                        "tool_use_id": item.call_id,
                        "content": text_content(item.content),
                        "is_error": item.is_error,
                    },
                )
            elif isinstance(item, ProviderReasoningItem):
                if item.provider != "anthropic":
                    raise ModelStreamError("reasoning continuation belongs to another provider")
                _append_message(messages, "assistant", item.provider_payload)

        kept_boundaries = {hint.boundary for hint in hints[:sent]}
        if "after_system" in kept_boundaries and system:
            system[-1]["cache_control"] = {"type": "ephemeral"}
        if "after_tools" in kept_boundaries and tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        if "after_history_prefix" in kept_boundaries and messages:
            content = messages[-1]["content"]
            if isinstance(content, list) and content:
                content[-1]["cache_control"] = {"type": "ephemeral"}

        payload: dict[str, Any] = {
            "model": resolved.model,
            "max_tokens": min(
                request.maximum_output_tokens or resolved.limits.default_output_reserve,
                resolved.limits.max_output_tokens,
            ),
            "system": system,
            "messages": messages,
            "tools": tools,
            "timeout": request.timeout_seconds,
        }
        if resolved.capabilities.reasoning.value == "native":
            payload["thinking"] = {"type": "adaptive"}
        return payload, sent, dropped

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await cast(Any, self._client).close()


def _append_message(messages: list[dict[str, Any]], role: str, block: dict[str, Any]) -> None:
    if messages and messages[-1]["role"] == role:
        content = messages[-1]["content"]
        if isinstance(content, list):
            content.append(block)
        return
    messages.append({"role": role, "content": [block]})


def _stop_reason(value: object) -> StopReason:
    return {
        "end_turn": StopReason.END_TURN,
        "tool_use": StopReason.TOOL_USE,
        "max_tokens": StopReason.MAX_TOKENS,
        "model_context_window_exceeded": StopReason.MAX_TOKENS,
        "stop_sequence": StopReason.STOP_SEQUENCE,
        "refusal": StopReason.CONTENT_FILTER,
        "pause_turn": StopReason.INCOMPLETE,
    }.get(str(value), StopReason.END_TURN)


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _integer(value: object, *, default: int = 0) -> int:
    return value if type(value) is int and value >= 0 else default


def _status_error_type(exc: APIStatusError) -> str | None:
    body = exc.body
    if not isinstance(body, dict):
        return None
    value = nested(body, "error", "type")
    return None if value is None else str(value)
