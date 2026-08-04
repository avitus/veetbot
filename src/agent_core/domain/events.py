"""Append-only event envelope types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter

from agent_core.domain.messages import (
    AssistantMessage,
    ContentPart,
    ConversationItem,
    TextPart,
    ToolResultItem,
    UserMessage,
)

CONVERSATION_ADAPTER: TypeAdapter[ConversationItem] = TypeAdapter(ConversationItem)
CONTENT_ADAPTER: TypeAdapter[list[ContentPart]] = TypeAdapter(list[ContentPart])
TOOL_RESULT_EVENTS = frozenset(
    {"tool.call.completed", "tool.call.failed", "tool.call.denied", "tool.call.uncertain"}
)


class NewEvent(BaseModel):
    session_id: UUID
    run_id: UUID | None
    event_type: str
    payload_schema_version: int = 1
    actor_type: str
    actor_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    derivation_key: str | None = None


class EventEnvelope(BaseModel):
    id: int
    session_id: UUID
    run_id: UUID | None
    sequence: int
    event_type: str
    payload_schema_version: int
    actor_type: str
    actor_id: str | None
    payload: dict[str, Any]
    trace_id: str | None
    derivation_key: str | None = None
    created_at: datetime


class ProcessEvent(BaseModel):
    """An append-only process-scoped event with no synthetic session identity."""

    id: UUID
    event_type: str
    payload_schema_version: int = 1
    actor_type: str
    actor_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    derivation_key: str
    created_at: datetime


def conversation_items(event: EventEnvelope) -> list[ConversationItem]:
    """Project content-bearing events into provider-neutral conversation items."""

    payload = event.payload
    if event.event_type == "user.message.created":
        content = payload.get("content")
        parts: list[ContentPart]
        if isinstance(content, str):
            parts = [TextPart(text=content)]
        elif isinstance(content, list):
            parts = CONTENT_ADAPTER.validate_python(content)
        else:
            raise ValueError(f"user.message.created payload has no content: {event.id}")
        return [
            UserMessage(
                content=parts,
                principal_id=event.actor_id,
                source_event_sequence=event.sequence,
            )
        ]
    if event.event_type == "assistant.message.completed":
        message = payload.get("message")
        if not isinstance(message, dict):
            raise ValueError(f"assistant.message.completed payload has no message: {event.id}")
        return [
            AssistantMessage.model_validate(message).model_copy(
                update={"source_event_sequence": event.sequence}
            )
        ]
    if event.event_type == "model.response.completed":
        raw_items = payload.get("conversation_items")
        if not isinstance(raw_items, list):
            raise ValueError(f"model.response.completed has no conversation items: {event.id}")
        return [
            CONVERSATION_ADAPTER.validate_python(item).model_copy(
                update={"source_event_sequence": event.sequence}
            )
            for item in raw_items
        ]
    if event.event_type in TOOL_RESULT_EVENTS:
        raw_result = payload.get("result_item")
        if not isinstance(raw_result, dict):
            raise ValueError(f"{event.event_type} has no result item: {event.id}")
        return [
            ToolResultItem.model_validate(raw_result).model_copy(
                update={"source_event_sequence": event.sequence}
            )
        ]
    return []
