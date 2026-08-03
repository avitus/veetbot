"""Append-only event envelope types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter

from agent_core.domain.messages import (
    AssistantMessage,
    ConversationItem,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)

CONVERSATION_ADAPTER: TypeAdapter[ConversationItem] = TypeAdapter(ConversationItem)


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


def conversation_items(event: EventEnvelope) -> list[ConversationItem]:
    """Project content-bearing events into provider-neutral conversation items."""

    payload = event.payload
    if event.event_type == "user.message.created":
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError(f"user.message.created payload has no string content: {event.id}")
        return [
            UserMessage(
                content=[TextPart(text=content)],
                principal_id=event.actor_id,
            )
        ]
    if event.event_type == "assistant.message.completed":
        message = payload.get("message")
        if not isinstance(message, dict):
            raise ValueError(f"assistant.message.completed payload has no message: {event.id}")
        return [AssistantMessage.model_validate(message)]
    raw_items = payload.get("conversation_items")
    if isinstance(raw_items, list):
        return [CONVERSATION_ADAPTER.validate_python(item) for item in raw_items]
    raw_call = payload.get("tool_call")
    if isinstance(raw_call, dict):
        return [ToolCallItem.model_validate(raw_call)]
    raw_result = payload.get("result_item")
    if isinstance(raw_result, dict):
        return [ToolResultItem.model_validate(raw_result)]
    return []
