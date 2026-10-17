"""Shared normalized-stream assembly and invariant enforcement."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from agent_core.domain.messages import (
    AssistantMessage,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelFailure,
    ModelTurn,
    ModelUsage,
    ProviderMetadata,
    ProviderReasoningItem,
    ReasoningDeltaEvent,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ToolCallDeltaEvent,
    ToolCallItem,
    ToolResultItem,
    UsageEvent,
)
from agent_core.domain.security import (
    CREDENTIAL_VALUE_CHARS_PATTERN,
    OPAQUE_BEARER_VALUE_PATTERN,
)

_DIAGNOSTIC_EVENT_KINDS = frozenset(
    {"text_delta", "reasoning_delta", "tool_call_delta", "usage", "completed", "failed", "unknown"}
)
_DIAGNOSTIC_DETECTORS = frozenset(
    {"authorization", "bearer", "provider_key", "private_key", "unknown"}
)
_DIAGNOSTIC_FIELDS = frozenset(
    {
        "event",
        "text",
        "turn",
        "partial_turn",
        "error",
        "assistant_messages",
        "content",
        "tool_calls",
        "arguments",
        "arguments_delta",
        "raw_arguments",
        "parse_error",
        "name",
        "call_id",
        "provider_reasoning_items",
        "provider_payload",
        "provider",
        "model",
        "provider_metadata",
        "usage",
        "stop_reason",
        "stop_sequence",
        "message",
        "provider_code",
        "provider_parameter",
        "encrypted_content",
        "signature",
        "data",
        "kind",
        "[]",
        "<key>",
        "<value>",
    }
)


def _safe_diagnostic_details(details: dict[str, str] | None) -> dict[str, str]:
    """Admit only the closed vocabulary, even if an adapter supplies diagnostics."""

    if details is None or set(details) != {
        "protocol_event_kind",
        "protocol_field_path",
        "protocol_detector",
    }:
        return {}
    if not all(isinstance(value, str) for value in details.values()):
        return {}
    path = details["protocol_field_path"].split(".")
    if (
        details["protocol_event_kind"] not in _DIAGNOSTIC_EVENT_KINDS
        or details["protocol_detector"] not in _DIAGNOSTIC_DETECTORS
        or len(path) > 12
        or any(part not in _DIAGNOSTIC_FIELDS for part in path)
    ):
        return {}
    return dict(details)


class ModelStreamError(ValueError):
    """A normalized adapter stream broke a provider-neutral invariant."""

    def __init__(
        self,
        message: str,
        *,
        failure: ModelFailure | None = None,
        partial_turn: ModelTurn | None = None,
        diagnostic_details: dict[str, str] | None = None,
    ) -> None:
        """Retain safe normalized failure detail for terminal diagnostics."""

        super().__init__(message)
        self.failure = failure
        self.partial_turn = partial_turn
        self._diagnostic_details = _safe_diagnostic_details(diagnostic_details)

    @property
    def diagnostic_details(self) -> dict[str, str]:
        """Return a fresh copy of the bounded, content-free refusal labels."""

        return dict(self._diagnostic_details)


SECRET_VALUE = re.compile(
    rf"(?i)(?:(?P<authorization>authorization\s*[:=]\s*(?:bearer\s+)?"
    rf"{CREDENTIAL_VALUE_CHARS_PATTERN}{{8,}})|"
    rf"(?P<bearer>bearer\s+{OPAQUE_BEARER_VALUE_PATTERN})|"
    r"(?P<provider_key>\bsk-(?:ant-|proj-)?[a-z0-9_-]{8,})|"
    r"(?P<private_key>-----begin [a-z ]*private key-----))"
)


def _credential_inspection_event(event: ModelEvent) -> ModelEvent:
    """Omit only ADR-0210's embedded OpenAI collisions from an inspection copy."""

    turn: ModelTurn | None
    if isinstance(event, ModelCompletedEvent):
        turn = event.turn
    elif isinstance(event, ModelFailedEvent) and event.error.provider == "openai":
        turn = event.partial_turn
    else:
        return event
    if (
        turn is None
        or turn.usage.provider != "openai"
        or turn.provider_metadata is None
        or turn.provider_metadata.provider_api != "responses"
    ):
        return event

    eligible: list[int] = []
    for index, item in enumerate(turn.provider_reasoning_items):
        value = item.provider_payload.get("encrypted_content")
        if (
            item.provider != "openai"
            or item.provider_payload.get("type") != "reasoning"
            or not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value) is None
        ):
            continue
        matches = list(SECRET_VALUE.finditer(value))
        if matches and all(
            match.lastgroup == "provider_key" and match.start() > 0 for match in matches
        ):
            eligible.append(index)
    if not eligible:
        return event

    inspection = event.model_copy(deep=True)
    inspected_turn = (
        inspection.turn if isinstance(inspection, ModelCompletedEvent) else inspection.partial_turn
    )
    assert inspected_turn is not None
    for index in eligible:
        inspected_turn.provider_reasoning_items[index].provider_payload["encrypted_content"] = ""
    return inspection


def _credential_field(
    value: object,
    path: tuple[str, ...] = (),
    payload_fields: frozenset[str] = frozenset(),
) -> str | None:
    """Locate a refused value using schema labels, never arbitrary mapping keys."""

    # Diagnostic precision is optional; bounded, content-free output is mandatory.
    if len(path) >= 12:
        return None
    if isinstance(value, str):
        if SECRET_VALUE.search(json.dumps(value, ensure_ascii=False)):
            return ".".join(path)
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            fields: frozenset[str] = frozenset()
            if isinstance(value, ProviderReasoningItem) and name == "provider_payload":
                # These names improve diagnostics only; admission is decided by
                # the separate, schema-bound inspection view above.
                fields = {
                    ("openai", "reasoning"): frozenset({"encrypted_content"}),
                    ("anthropic", "thinking"): frozenset({"signature"}),
                    ("anthropic", "redacted_thinking"): frozenset({"data"}),
                }.get((value.provider, str(value.provider_payload.get("type"))), frozenset())
            label = name if name in _DIAGNOSTIC_FIELDS else "<value>"
            found = _credential_field(getattr(value, name), (*path, label), fields)
            if found is not None:
                return found
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and SECRET_VALUE.search(json.dumps(key, ensure_ascii=False)):
                return ".".join((*path, "<key>"))
            label = key if isinstance(key, str) and key in payload_fields else "<value>"
            found = _credential_field(item, (*path, label))
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _credential_field(item, (*path, "[]"))
            if found is not None:
                return found
    return None


def _credential_diagnostics(event: ModelEvent, match: re.Match[str]) -> dict[str, str]:
    """Describe a refusal without retaining the event, matched bytes, or key names."""

    kinds = {
        TextDeltaEvent: "text_delta",
        ReasoningDeltaEvent: "reasoning_delta",
        ToolCallDeltaEvent: "tool_call_delta",
        UsageEvent: "usage",
        ModelCompletedEvent: "completed",
        ModelFailedEvent: "failed",
    }
    return {
        "protocol_event_kind": kinds.get(type(event), "unknown"),
        "protocol_field_path": _credential_field(event) or "event",
        "protocol_detector": match.lastgroup or "unknown",
    }


@dataclass(slots=True)
class _ToolBuffer:
    call_id: str
    name: str
    raw_arguments: str = ""


@dataclass(slots=True)
class ModelStreamAccumulator:
    """Fold normalized deltas into the authoritative terminal turn."""

    text: dict[int, str] = field(default_factory=dict)
    tools: dict[int, _ToolBuffer] = field(default_factory=dict)

    def add(self, event: ModelEvent) -> None:
        if isinstance(event, TextDeltaEvent):
            self.text[event.item_index] = self.text.get(event.item_index, "") + event.text
        elif isinstance(event, ToolCallDeltaEvent):
            if event.call_id is None or event.name is None:
                raise ModelStreamError("tool identity must be known at item start")
            buffer = self.tools.get(event.item_index)
            if buffer is None:
                buffer = _ToolBuffer(call_id=event.call_id, name=event.name)
                self.tools[event.item_index] = buffer
            elif buffer.call_id != event.call_id or buffer.name != event.name:
                raise ModelStreamError("tool identity changed within one output item")
            buffer.raw_arguments += event.arguments_delta

    def turn(
        self,
        *,
        usage: ModelUsage,
        stop_reason: StopReason,
        metadata: ProviderMetadata | None = None,
        reasoning_items: list[ProviderReasoningItem] | None = None,
    ) -> ModelTurn:
        assistant_messages = [
            AssistantMessage(content=[TextPart(text=value)], item_index=index)
            for index, value in sorted(self.text.items())
            if value
        ]
        tool_calls = [self._close_tool(index, value) for index, value in sorted(self.tools.items())]
        return ModelTurn(
            assistant_messages=assistant_messages,
            tool_calls=tool_calls,
            provider_reasoning_items=list(reasoning_items or []),
            usage=usage,
            stop_reason=stop_reason,
            provider_metadata=metadata,
        )

    @staticmethod
    def _close_tool(item_index: int, buffer: _ToolBuffer) -> ToolCallItem:
        arguments: dict[str, Any] = {}
        parse_error: str | None = None
        if buffer.raw_arguments.strip():
            try:
                parsed = json.loads(buffer.raw_arguments)
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    parse_error = "arguments were not an object"
            except json.JSONDecodeError:
                parse_error = "arguments were not valid JSON"
        return ToolCallItem(
            call_id=buffer.call_id,
            item_index=item_index,
            name=buffer.name,
            arguments=arguments,
            raw_arguments=buffer.raw_arguments,
            parse_error=parse_error,
        )


def validate_conversation_pairing(items: Sequence[object]) -> None:
    """Reject dangling, duplicate, or orphaned tool-call history before provider I/O."""

    calls: dict[str, int] = {}
    results: set[str] = set()
    for position, item in enumerate(items):
        if isinstance(item, ToolCallItem):
            if item.call_id in calls:
                raise ModelStreamError(f"duplicate tool call id at conversation item {position}")
            calls[item.call_id] = position
        elif isinstance(item, ToolResultItem):
            if item.call_id not in calls:
                raise ModelStreamError(f"orphan tool result at conversation item {position}")
            if item.call_id in results:
                raise ModelStreamError(f"duplicate tool result at conversation item {position}")
            results.add(item.call_id)
    dangling = set(calls) - results
    if dangling:
        raise ModelStreamError("dangling tool call has no matching result")


async def validated_stream(source: AsyncIterable[ModelEvent]) -> AsyncIterator[ModelEvent]:
    """Validate all six normalized stream invariants while preserving streaming."""

    expected_sequence = 0
    terminal_seen = False
    closed_items: set[int] = set()
    active_item: int | None = None
    active_kind: str | None = None
    tool_identity: dict[int, tuple[str, str]] = {}

    async for event in source:
        if terminal_seen:
            raise ModelStreamError("an event followed the terminal event")
        if event.sequence != expected_sequence:
            raise ModelStreamError("stream sequence did not start at zero and remain gapless")
        expected_sequence += 1
        if credential_match := SECRET_VALUE.search(event.model_dump_json()):
            inspection = _credential_inspection_event(event)
            if credential_match := SECRET_VALUE.search(inspection.model_dump_json()):
                raise ModelStreamError(
                    "normalized event carried credential-shaped content",
                    diagnostic_details=_credential_diagnostics(inspection, credential_match),
                )

        if isinstance(event, (TextDeltaEvent, ReasoningDeltaEvent, ToolCallDeltaEvent)):
            item_kind = event.kind
            if active_item is None:
                if event.item_index in closed_items:
                    raise ModelStreamError("deltas for one item were not contiguous")
                active_item = event.item_index
                active_kind = item_kind
            elif event.item_index != active_item:
                closed_items.add(active_item)
                if event.item_index in closed_items:
                    raise ModelStreamError("deltas for one item were not contiguous")
                active_item = event.item_index
                active_kind = item_kind
            elif active_kind != item_kind:
                raise ModelStreamError("one output item changed delta kind")

            if isinstance(event, ToolCallDeltaEvent):
                if event.call_id is None or event.name is None:
                    raise ModelStreamError("tool identity was absent at item start")
                identity = (event.call_id, event.name)
                previous = tool_identity.setdefault(event.item_index, identity)
                if previous != identity:
                    raise ModelStreamError("tool identity changed within one output item")
        elif isinstance(event, UsageEvent):
            if event.is_final:
                raise ModelStreamError("usage events must remain provisional")
        elif isinstance(event, (ModelCompletedEvent, ModelFailedEvent)):
            terminal_seen = True
        else:
            raise ModelStreamError("unknown normalized event type")

        yield event

    if not terminal_seen:
        raise ModelStreamError("stream ended without exactly one terminal event")


async def collect_turn(source: AsyncIterable[ModelEvent]) -> ModelTurn:
    """Collect a validated stream and return its authoritative terminal turn."""

    terminal: ModelCompletedEvent | ModelFailedEvent | None = None
    async for event in validated_stream(source):
        if isinstance(event, (ModelCompletedEvent, ModelFailedEvent)):
            terminal = event
    if isinstance(terminal, ModelCompletedEvent):
        return terminal.turn
    if isinstance(terminal, ModelFailedEvent):
        raise ModelStreamError(
            f"model attempt failed: {terminal.error.kind}",
            failure=terminal.error,
            partial_turn=terminal.partial_turn,
        )
    raise ModelStreamError("stream did not produce a terminal turn")
