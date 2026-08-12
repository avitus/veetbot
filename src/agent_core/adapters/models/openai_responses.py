"""OpenAI Responses adapter; the only module that imports the OpenAI SDK."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

from agent_core.adapters.models.common import (
    RawEventSource,
    as_mapping,
    failed_event,
    nested,
    text_content,
    tool_definition,
)
from agent_core.adapters.models.registry import OPENAI_CAPABILITY_CEILING
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
    sanitize_provider_code,
    sanitize_provider_parameter,
)
from agent_core.model.cost import price_usage
from agent_core.model.streaming import ModelStreamAccumulator, ModelStreamError

_OPENAI_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
logger = logging.getLogger(__name__)


def _wire_tool_name(name: str) -> str:
    if _OPENAI_TOOL_NAME.fullmatch(name):
        return name
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:31]
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    return f"{stem}_{digest}"


def _tool_name_maps(request: ModelRequest) -> tuple[dict[str, str], dict[str, str]]:
    canonical_to_wire: dict[str, str] = {}
    wire_to_canonical: dict[str, str] = {}
    for tool in request.tools:
        wire_name = _wire_tool_name(tool.name)
        existing = wire_to_canonical.get(wire_name)
        if existing is not None and existing != tool.name:
            raise ValueError("OpenAI tool-name aliases collided")
        canonical_to_wire[tool.name] = wire_name
        wire_to_canonical[wire_name] = tool.name
    return canonical_to_wire, wire_to_canonical


def _status_error_field(error: APIStatusError, field: str) -> str | None:
    body = error.body
    if not isinstance(body, dict):
        return None
    nested_error = body.get("error", body)
    if not isinstance(nested_error, dict):
        return None
    value = nested_error.get(field)
    return value if isinstance(value, str) else None


class OpenAIResponsesProvider:
    """Translate official Responses stream events into the neutral contract."""

    name = "openai"
    CAPABILITY_CEILING = OPENAI_CAPABILITY_CEILING

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        event_source: RawEventSource | None = None,
        client: object | None = None,
        max_internal_attempts: int = 3,
    ) -> None:
        self._event_source = event_source
        self._client = client
        self._owns_client = client is None and event_source is None
        self._base_url = base_url
        self._api_key = api_key
        self._max_internal_attempts = max_internal_attempts
        if self._owns_client:
            if not api_key:
                raise ValueError("OpenAI provider requires its resolved credential")
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    async def _sdk_events(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError("OpenAI client is unavailable")
        responses = cast(Any, self._client).responses
        stream = await responses.create(**payload, stream=True)
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
            payload = self._request_payload(request, resolved)
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
                    self._source(payload), request, resolved, attempt
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
                transient = exc.status_code == 429 or exc.status_code >= 500
                if (
                    transient
                    and emitted_count == 0
                    and internal_attempt < self._max_internal_attempts
                ):
                    continue
                provider_code = (
                    sanitize_provider_code(_status_error_field(exc, "code"))
                    or f"http_{exc.status_code}"
                )
                provider_parameter = sanitize_provider_parameter(_status_error_field(exc, "param"))
                logger.warning(
                    "openai_request_rejected status=%s code=%s parameter=%s",
                    exc.status_code,
                    provider_code,
                    provider_parameter or "none",
                )
                yield failed_event(
                    attempt=attempt,
                    provider=self.name,
                    model=resolved.model,
                    sequence=emitted_count,
                    category="transient" if transient else "permanent",
                    provider_code=provider_code,
                    http_status=exc.status_code,
                    provider_parameter=provider_parameter,
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
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        try:
            _, wire_to_canonical = _tool_name_maps(request)
        except ValueError:
            yield failed_event(
                attempt=attempt,
                provider=self.name,
                model=resolved.model,
                sequence=0,
                category="protocol",
                detail="OpenAI tool-name aliases collided",
            )
            return
        sequence = 0
        accumulator = ModelStreamAccumulator()
        tool_identity: dict[int, tuple[str, str]] = {}
        reasoning_items: list[ProviderReasoningItem] = []
        response_id: str | None = None
        terminal = False

        async for raw in source:
            event_type = raw.get("type")
            response_id = _optional_string(raw.get("response_id")) or response_id
            if event_type in {"response.created", "response.in_progress"}:
                response = raw.get("response")
                if isinstance(response, dict):
                    response_id = _optional_string(response.get("id")) or response_id
            if event_type == "response.output_item.added":
                item = raw.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    item_index = int(raw.get("output_index", 0))
                    call_id = str(item.get("call_id", ""))
                    provider_name = str(item.get("name", ""))
                    name = wire_to_canonical.get(provider_name, provider_name)
                    tool_identity[item_index] = (call_id, name)
                    tool_event = ToolCallDeltaEvent(
                        attempt_id=attempt.attempt_id,
                        run_id=attempt.run_id,
                        step_number=attempt.step_number,
                        sequence=sequence,
                        item_index=item_index,
                        call_id=call_id,
                        name=name,
                        arguments_delta="",
                    )
                    accumulator.add(tool_event)
                    yield tool_event
                    sequence += 1
            elif event_type == "response.output_text.delta":
                text_event = TextDeltaEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=sequence,
                    item_index=int(raw.get("output_index", 0)),
                    text=str(raw.get("delta", "")),
                )
                accumulator.add(text_event)
                yield text_event
                sequence += 1
            elif event_type in {
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            }:
                yield ReasoningDeltaEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=sequence,
                    item_index=int(raw.get("output_index", 0)),
                    text=str(raw.get("delta", "")),
                    is_summary=event_type == "response.reasoning_summary_text.delta",
                )
                sequence += 1
            elif event_type == "response.function_call_arguments.delta":
                item_index = int(raw.get("output_index", 0))
                identity = tool_identity.get(item_index)
                if identity is None:
                    yield failed_event(
                        attempt=attempt,
                        provider=self.name,
                        model=resolved.model,
                        sequence=sequence,
                        category="protocol",
                        detail="function arguments arrived before the function item",
                    )
                    return
                tool_event = ToolCallDeltaEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=sequence,
                    item_index=item_index,
                    call_id=identity[0],
                    name=identity[1],
                    arguments_delta=str(raw.get("delta", "")),
                )
                accumulator.add(tool_event)
                yield tool_event
                sequence += 1
            elif event_type == "response.output_item.done":
                item = raw.get("item")
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    opaque = {
                        key: item[key]
                        for key in ("id", "type", "encrypted_content", "status")
                        if key in item
                    }
                    # Responses requires this array when the reasoning item is
                    # replayed as input. Summaries are not requested, so retain
                    # the required empty field without persisting reasoning text.
                    opaque["summary"] = []
                    if response_id is not None:
                        opaque["response_id"] = response_id
                    if opaque:
                        reasoning_items.append(
                            ProviderReasoningItem(
                                item_index=int(raw.get("output_index", 0)),
                                provider=self.name,
                                provider_payload=opaque,
                            )
                        )
            elif event_type == "response.completed":
                response = raw.get("response")
                if not isinstance(response, dict):
                    response = {}
                response_id = _optional_string(response.get("id")) or response_id
                if response_id is not None:
                    for reasoning_item in reasoning_items:
                        reasoning_item.provider_payload.setdefault("response_id", response_id)
                usage = self._usage(response, resolved)
                stop_reason = self._stop_reason(response, accumulator)
                metadata = ProviderMetadata(
                    provider_api="responses",
                    response_id=response_id,
                    request_id=_optional_string(
                        raw.get("request_id") or response.get("request_id")
                    ),
                    resolved_model=_optional_string(response.get("model")),
                    previous_response_id=_optional_string(payload_previous_id(request)),
                    cache_breakpoints_sent=0,
                    cache_breakpoints_dropped=len(
                        [] if request.cache_hints is None else request.cache_hints.breakpoints
                    ),
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
                )
                return
            elif event_type in {"response.failed", "error"}:
                code = nested(raw, "response", "error", "code") or nested(raw, "error", "code")
                parameter = nested(raw, "response", "error", "param") or nested(
                    raw, "error", "param"
                )
                yield failed_event(
                    attempt=attempt,
                    provider=self.name,
                    model=resolved.model,
                    sequence=sequence,
                    category="permanent",
                    provider_code=None if code is None else str(code),
                    provider_parameter=None if parameter is None else str(parameter),
                    stream_had_output=sequence > 0,
                )
                terminal = True
                return
        if not terminal:
            yield failed_event(
                attempt=attempt,
                provider=self.name,
                model=resolved.model,
                sequence=sequence,
                category="protocol",
                detail="Responses stream ended without response.completed",
            )

    @staticmethod
    def _usage(response: dict[str, Any], resolved: ResolvedModel) -> ModelUsage:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = max(0, int(usage.get("input_tokens", 0)))
        cached_input_tokens = min(
            input_tokens,
            max(0, int(nested(usage, "input_tokens_details", "cached_tokens", default=0))),
        )
        output_tokens = max(0, int(usage.get("output_tokens", 0)))
        reasoning_tokens = min(
            output_tokens,
            max(
                0,
                int(nested(usage, "output_tokens_details", "reasoning_tokens", default=0)),
            ),
        )
        normalized = ModelUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            provider="openai",
            model=resolved.model,
        )
        return price_usage(normalized, resolved.pricing)

    @staticmethod
    def _stop_reason(response: dict[str, Any], accumulator: ModelStreamAccumulator) -> StopReason:
        if accumulator.tools:
            return StopReason.TOOL_USE
        status = response.get("status")
        if status == "incomplete":
            reason = nested(response, "incomplete_details", "reason")
            return StopReason.MAX_TOKENS if reason == "max_output_tokens" else StopReason.INCOMPLETE
        if status in {"failed", "cancelled"}:
            return StopReason.CANCELLED if status == "cancelled" else StopReason.INCOMPLETE
        return StopReason.END_TURN

    @staticmethod
    def _request_payload(request: ModelRequest, resolved: ResolvedModel) -> dict[str, Any]:
        from agent_core.model.streaming import validate_conversation_pairing

        validate_conversation_pairing(request.conversation)
        canonical_to_wire, _ = _tool_name_maps(request)
        inputs: list[dict[str, Any]] = []
        for item in request.conversation:
            if isinstance(item, (SystemMessage, UserMessage, AssistantMessage)):
                inputs.append(
                    {
                        "role": item.kind,
                        "content": text_content(item.content),
                    }
                )
            elif isinstance(item, ToolCallItem):
                inputs.append(
                    {
                        "type": "function_call",
                        "call_id": item.call_id,
                        "name": canonical_to_wire.get(item.name, _wire_tool_name(item.name)),
                        "arguments": item.raw_arguments,
                    }
                )
            elif isinstance(item, ToolResultItem):
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": text_content(item.content),
                    }
                )
            elif isinstance(item, ProviderReasoningItem):
                if item.provider != "openai":
                    raise ModelStreamError("reasoning continuation belongs to another provider")
                provider_payload = dict(item.provider_payload)
                provider_payload.pop("response_id", None)
                inputs.append(provider_payload)
        tool_definitions: list[dict[str, Any]] = []
        for tool in request.tools:
            definition = tool_definition(tool)
            definition["name"] = canonical_to_wire[tool.name]
            definition["strict"] = False
            tool_definitions.append(definition)
        payload: dict[str, Any] = {
            "model": resolved.model,
            "input": inputs,
            "tools": tool_definitions,
            "store": False,
            "timeout": request.timeout_seconds,
        }
        if request.maximum_output_tokens is not None:
            payload["max_output_tokens"] = min(
                request.maximum_output_tokens, resolved.limits.max_output_tokens
            )
        if request.response_schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "agent_response",
                    "strict": True,
                    "schema": request.response_schema,
                }
            }
        return payload

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await cast(Any, self._client).close()


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def payload_previous_id(request: ModelRequest) -> str | None:
    for item in reversed(request.conversation):
        if isinstance(item, ProviderReasoningItem) and item.provider == "openai":
            value = item.provider_payload.get("response_id")
            if value is not None:
                return str(value)
    return None
