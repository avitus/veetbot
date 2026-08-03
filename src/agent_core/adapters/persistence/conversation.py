"""Storage-neutral event-to-conversation projection rules."""

from __future__ import annotations

from pydantic import TypeAdapter

from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import (
    AssistantMessage,
    ConversationItem,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)

CONVERSATION_ADAPTER: TypeAdapter[ConversationItem] = TypeAdapter(ConversationItem)


def conversation_items(event: EventEnvelope) -> list[ConversationItem]:
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
