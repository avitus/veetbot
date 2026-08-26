"""Shared normalized-stream assembly and invariant enforcement."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

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


class ModelStreamError(ValueError):
    """A normalized adapter stream broke a provider-neutral invariant."""

    def __init__(self, message: str, *, failure: ModelFailure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


_TOKEN_CHARS = r"[a-z0-9._~+/=-]"
_BARE_BEARER_VALUE = (
    rf"(?:[a-z0-9_-]{{16,}}\.[a-z0-9_-]{{6,}}\.[a-z0-9_-]{{6,}}|"
    rf"{_TOKEN_CHARS}{{32,}})"
)
SECRET_VALUE = re.compile(
    rf"(?i)(?:authorization\s*[:=]\s*(?:bearer\s+)?{_TOKEN_CHARS}{{8,}}|"
    rf"bearer\s+{_BARE_BEARER_VALUE}|"
    r"sk-(?:ant-|proj-)?[a-z0-9_-]{8,}|-----begin [a-z ]*private key-----)"
)


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
        if SECRET_VALUE.search(event.model_dump_json()):
            raise ModelStreamError("normalized event carried credential-shaped content")

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
        )
    raise ModelStreamError("stream did not produce a terminal turn")
