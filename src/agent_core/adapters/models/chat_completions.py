"""SDK-free OpenAI-compatible chat-completions streaming adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agent_core.adapters.models.common import RawEventSource, failed_event, text_content
from agent_core.adapters.models.registry import CHAT_COMPLETIONS_CAPABILITY_CEILING
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
    UserMessage,
)
from agent_core.domain.tools import ToolSpec
from agent_core.model.cost import price_usage
from agent_core.model.streaming import ModelStreamAccumulator, ModelStreamError


class ThinkScrubber:
    """Boundary-gated state machine for tags split across arbitrary chunks."""

    def __init__(self, open_tag: str = "<think>", close_tag: str = "</think>") -> None:
        if not open_tag or not close_tag:
            raise ValueError("reasoning tags must be non-empty")
        self._open = open_tag
        self._close = close_tag
        self._inside = False
        self._buffer = ""

    def feed(self, chunk: str) -> list[tuple[bool, str]]:
        self._buffer += chunk
        output: list[tuple[bool, str]] = []
        while self._buffer:
            boundary = self._close if self._inside else self._open
            index = self._buffer.find(boundary)
            if index >= 0:
                if index:
                    output.append((self._inside, self._buffer[:index]))
                self._buffer = self._buffer[index + len(boundary) :]
                self._inside = not self._inside
                continue
            retained = _longest_boundary_prefix(self._buffer, boundary)
            emit_until = len(self._buffer) - retained
            if emit_until:
                output.append((self._inside, self._buffer[:emit_until]))
                self._buffer = self._buffer[emit_until:]
            break
        return output

    def finish(self) -> list[tuple[bool, str]]:
        if not self._buffer:
            return []
        output = [(self._inside, self._buffer)]
        self._buffer = ""
        return output


class XmlToolCallParser:
    """Remove complete XML tool envelopes and retain their raw JSON payloads."""

    OPEN = "<tool_call>"
    CLOSE = "</tool_call>"

    def __init__(self) -> None:
        self._inside = False
        self._buffer = ""
        self._payload = ""

    def feed(self, chunk: str) -> tuple[list[str], list[str]]:
        self._buffer += chunk
        visible: list[str] = []
        calls: list[str] = []
        while self._buffer:
            boundary = self.CLOSE if self._inside else self.OPEN
            index = self._buffer.find(boundary)
            if index >= 0:
                before = self._buffer[:index]
                if self._inside:
                    self._payload += before
                    calls.append(self._payload)
                    self._payload = ""
                elif before:
                    visible.append(before)
                self._buffer = self._buffer[index + len(boundary) :]
                self._inside = not self._inside
                continue
            retained = _longest_boundary_prefix(self._buffer, boundary)
            emit_until = len(self._buffer) - retained
            if emit_until:
                value = self._buffer[:emit_until]
                if self._inside:
                    self._payload += value
                else:
                    visible.append(value)
                self._buffer = self._buffer[emit_until:]
            break
        return visible, calls

    def finish(self) -> tuple[list[str], list[str]]:
        value = self.OPEN + self._payload + self._buffer if self._inside else self._buffer
        self._inside = False
        self._payload = ""
        self._buffer = ""
        return ([value] if value else []), []


class ChatCompletionsProvider:
    """Translate OpenAI-compatible SSE, including local Ollama streams."""

    name = "chat_completions"
    CAPABILITY_CEILING = CHAT_COMPLETIONS_CAPABILITY_CEILING

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        event_source: RawEventSource | None = None,
        client: httpx.AsyncClient | None = None,
        think_open: str = "<think>",
        think_close: str = "</think>",
        max_internal_attempts: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._event_source = event_source
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(600, read=60))
        self._owns_client = client is None
        self._think_open = think_open
        self._think_close = think_close
        self._max_internal_attempts = max_internal_attempts

    async def _http_events(
        self, payload: dict[str, Any], timeout: httpx.Timeout
    ) -> AsyncIterator[dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    yield {"type": "done"}
                    return
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    yield {"type": "malformed_sse"}
                    return
                if isinstance(parsed, dict):
                    yield {str(key): value for key, value in parsed.items()}

    def _source(
        self, payload: dict[str, Any], timeout: httpx.Timeout
    ) -> AsyncIterator[dict[str, Any]]:
        if self._event_source is not None:
            return self._event_source(payload)
        return self._http_events(payload, timeout)

    async def stream(
        self,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        try:
            payload, timeout = self._request_payload(request, resolved)
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
                    self._source(dict(payload), timeout), resolved, attempt
                ):
                    emitted_count = event.sequence + 1
                    yield event
                return
            except httpx.TransportError:
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
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                transient = status == 429 or status >= 500
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
                    provider_code=f"http_{status}",
                    http_status=status,
                    stream_had_output=emitted_count > 0,
                )
                return

    async def _translate(
        self,
        source: AsyncIterator[dict[str, Any]],
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        sequence = 0
        accumulator = ModelStreamAccumulator()
        think = ThinkScrubber(self._think_open, self._think_close)
        xml = None if resolved.capabilities.native_tool_calling else XmlToolCallParser()
        native_identity: dict[int, tuple[str, str]] = {}
        native_items: dict[int, int] = {}
        response_id: str | None = None
        response_model: str | None = None
        request_id: str | None = None
        usage_raw: dict[str, Any] = {}
        finish_reason: object = None
        terminal = False
        next_item_index = 0
        active_kind: str | None = None
        active_item_index: int | None = None

        def item_index(kind: str) -> int:
            nonlocal active_item_index, active_kind, next_item_index
            if active_kind != kind or active_item_index is None:
                active_kind = kind
                active_item_index = next_item_index
                next_item_index += 1
            return active_item_index

        async for raw in source:
            if raw.get("type") == "malformed_sse":
                yield failed_event(
                    attempt=attempt,
                    provider=self.name,
                    model=resolved.model,
                    sequence=sequence,
                    category="protocol",
                    detail="chat-completions endpoint emitted malformed SSE JSON",
                )
                return
            if raw.get("type") == "done":
                pieces = think.finish()
                for is_reasoning, value in pieces:
                    emitted_events, next_item_index, active_kind, active_item_index = (
                        self._content_events(
                            value,
                            is_reasoning=is_reasoning,
                            xml=xml,
                            attempt=attempt,
                            sequence=sequence,
                            accumulator=accumulator,
                            next_item_index=next_item_index,
                            active_kind=active_kind,
                            active_item_index=active_item_index,
                        )
                    )
                    for event in emitted_events:
                        yield event
                        sequence += 1
                visible, calls = ([], []) if xml is None else xml.finish()
                for value in visible:
                    index = item_index("text")
                    text_event = TextDeltaEvent(
                        attempt_id=attempt.attempt_id,
                        run_id=attempt.run_id,
                        step_number=attempt.step_number,
                        sequence=sequence,
                        item_index=index,
                        text=value,
                    )
                    accumulator.add(text_event)
                    yield text_event
                    sequence += 1
                for payload in calls:
                    tool_event = self._xml_event(
                        payload, attempt=attempt, sequence=sequence, item_index=next_item_index
                    )
                    next_item_index += 1
                    active_kind = None
                    active_item_index = None
                    accumulator.add(tool_event)
                    yield tool_event
                    sequence += 1
                usage = self._usage(usage_raw, resolved)
                stop_reason = _stop_reason(finish_reason, bool(accumulator.tools))
                metadata = ProviderMetadata(
                    provider_api="chat_completions",
                    response_id=response_id,
                    request_id=request_id,
                    resolved_model=response_model,
                )
                turn = accumulator.turn(
                    usage=usage,
                    stop_reason=stop_reason,
                    metadata=metadata,
                )
                yield ModelCompletedEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=sequence,
                    turn=turn,
                    stop_reason=stop_reason,
                )
                return

            response_id = _optional_string(raw.get("id")) or response_id
            response_model = _optional_string(raw.get("model")) or response_model
            request_id = _optional_string(raw.get("request_id")) or request_id
            if isinstance(raw.get("usage"), dict):
                usage_raw = raw["usage"]
            choices = raw.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str) and content:
                    for is_reasoning, value in think.feed(content):
                        events, next_item_index, active_kind, active_item_index = (
                            self._content_events(
                                value,
                                is_reasoning=is_reasoning,
                                xml=xml,
                                attempt=attempt,
                                sequence=sequence,
                                accumulator=accumulator,
                                next_item_index=next_item_index,
                                active_kind=active_kind,
                                active_item_index=active_item_index,
                            )
                        )
                        for event in events:
                            yield event
                            sequence += 1
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    active_kind = None
                    active_item_index = None
                    for tool_call in tool_calls:
                        if not isinstance(tool_call, dict):
                            continue
                        provider_index = int(tool_call.get("index", 0))
                        function = tool_call.get("function")
                        if not isinstance(function, dict):
                            function = {}
                        previous = native_identity.get(provider_index, ("", ""))
                        identity = (
                            str(tool_call.get("id") or previous[0]),
                            str(function.get("name") or previous[1]),
                        )
                        native_identity[provider_index] = identity
                        item = native_items.get(provider_index)
                        if item is None:
                            item = next_item_index
                            native_items[provider_index] = item
                            next_item_index += 1
                        event = ToolCallDeltaEvent(
                            attempt_id=attempt.attempt_id,
                            run_id=attempt.run_id,
                            step_number=attempt.step_number,
                            sequence=sequence,
                            item_index=item,
                            call_id=identity[0],
                            name=identity[1],
                            arguments_delta=str(function.get("arguments", "")),
                        )
                        accumulator.add(event)
                        yield event
                        sequence += 1
        if not terminal:
            yield failed_event(
                attempt=attempt,
                provider=self.name,
                model=resolved.model,
                sequence=sequence,
                category="protocol",
                detail="chat-completions stream ended without [DONE]",
            )

    def _content_events(
        self,
        value: str,
        *,
        is_reasoning: bool,
        xml: XmlToolCallParser | None,
        attempt: ModelAttempt,
        sequence: int,
        accumulator: ModelStreamAccumulator,
        next_item_index: int,
        active_kind: str | None,
        active_item_index: int | None,
    ) -> tuple[list[ModelEvent], int, str | None, int | None]:
        events: list[ModelEvent] = []
        if is_reasoning:
            if active_kind != "reasoning" or active_item_index is None:
                active_kind = "reasoning"
                active_item_index = next_item_index
                next_item_index += 1
            events.append(
                ReasoningDeltaEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=sequence,
                    item_index=active_item_index,
                    text=value,
                    is_summary=False,
                )
            )
            return events, next_item_index, active_kind, active_item_index

        visible, calls = ([value], []) if xml is None else xml.feed(value)
        for text in visible:
            if active_kind != "text" or active_item_index is None:
                active_kind = "text"
                active_item_index = next_item_index
                next_item_index += 1
            text_event = TextDeltaEvent(
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                step_number=attempt.step_number,
                sequence=sequence + len(events),
                item_index=active_item_index,
                text=text,
            )
            accumulator.add(text_event)
            events.append(text_event)
        for payload in calls:
            active_kind = None
            active_item_index = None
            tool_event = self._xml_event(
                payload,
                attempt=attempt,
                sequence=sequence + len(events),
                item_index=next_item_index,
            )
            next_item_index += 1
            accumulator.add(tool_event)
            events.append(tool_event)
        return events, next_item_index, active_kind, active_item_index

    @staticmethod
    def _xml_event(
        payload: str, *, attempt: ModelAttempt, sequence: int, item_index: int
    ) -> ToolCallDeltaEvent:
        name = "invalid_tool_call"
        call_id = f"xml-{attempt.step_number}-{item_index}"
        raw_arguments = payload
        try:
            decoded = json.loads(payload)
            if isinstance(decoded, dict):
                name = str(decoded.get("name", name))
                call_id = str(decoded.get("id", call_id))
                arguments = decoded.get("arguments", {})
                raw_arguments = (
                    arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments, separators=(",", ":"), sort_keys=True)
                )
        except json.JSONDecodeError:
            pass
        return ToolCallDeltaEvent(
            attempt_id=attempt.attempt_id,
            run_id=attempt.run_id,
            step_number=attempt.step_number,
            sequence=sequence,
            item_index=item_index,
            call_id=call_id,
            name=name,
            arguments_delta=raw_arguments,
        )

    @staticmethod
    def _usage(raw: dict[str, Any], resolved: ResolvedModel) -> ModelUsage:
        prompt_details = raw.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        input_tokens = max(0, int(raw.get("prompt_tokens", 0)))
        cached_input_tokens = min(
            input_tokens,
            max(0, int(prompt_details.get("cached_tokens", 0))),
        )
        normalized = ModelUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=max(0, int(raw.get("completion_tokens", 0))),
            reasoning_tokens=None,
            provider="chat_completions",
            model=resolved.model,
        )
        return price_usage(normalized, resolved.pricing)

    @staticmethod
    def _request_payload(
        request: ModelRequest, resolved: ResolvedModel
    ) -> tuple[dict[str, Any], httpx.Timeout]:
        from agent_core.model.streaming import validate_conversation_pairing

        validate_conversation_pairing(request.conversation)
        messages: list[dict[str, Any]] = []
        for item in request.conversation:
            if isinstance(item, (SystemMessage, UserMessage, AssistantMessage)):
                messages.append({"role": item.kind, "content": text_content(item.content)})
            elif isinstance(item, ToolCallItem):
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": item.call_id,
                                "type": "function",
                                "function": {"name": item.name, "arguments": item.raw_arguments},
                            }
                        ],
                    }
                )
            elif isinstance(item, ToolResultItem):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.call_id,
                        "content": text_content(item.content),
                    }
                )
            elif isinstance(item, ProviderReasoningItem):
                raise ModelStreamError("chat-completions cannot replay provider reasoning state")
        tools: list[dict[str, Any]] = []
        for value in request.tools:
            tool = ToolSpec.model_validate(value)
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
            )
        if not resolved.capabilities.native_tool_calling and tools:
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "When a tool is needed emit exactly "
                        '<tool_call>{"name":"tool.name","arguments":{}}</tool_call>. '
                        f"Available tool schemas: {json.dumps(tools, sort_keys=True)}"
                    ),
                },
            )
        payload: dict[str, Any] = {
            "model": resolved.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if resolved.capabilities.native_tool_calling:
            payload["tools"] = tools
        if request.maximum_output_tokens is not None:
            payload["max_tokens"] = min(
                request.maximum_output_tokens, resolved.limits.max_output_tokens
            )
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload, httpx.Timeout(
            request.timeout_seconds,
            read=request.stream_idle_seconds,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _longest_boundary_prefix(value: str, boundary: str) -> int:
    maximum = min(len(value), len(boundary) - 1)
    for size in range(maximum, 0, -1):
        if value.endswith(boundary[:size]):
            return size
    return 0


def _stop_reason(value: object, has_tools: bool) -> StopReason:
    if has_tools or value in {"tool_calls", "function_call"}:
        return StopReason.TOOL_USE
    return {
        "stop": StopReason.END_TURN,
        "length": StopReason.MAX_TOKENS,
        "content_filter": StopReason.CONTENT_FILTER,
    }.get(str(value), StopReason.END_TURN)


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)
