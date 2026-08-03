"""Storage-neutral event-to-conversation projection rules."""

from __future__ import annotations

from pydantic import TypeAdapter

from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import (
    AssistantMessage,
    ConversationItem,
    TextPart,
    ToolResultItem,
    UserMessage,
)

CONVERSATION_ADAPTER: TypeAdapter[ConversationItem] = TypeAdapter(ConversationItem)
TOOL_RESULT_EVENTS = frozenset(
    {"tool.call.completed", "tool.call.failed", "tool.call.denied", "tool.call.uncertain"}
)


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
    if event.event_type == "model.response.completed":
        raw_items = payload.get("conversation_items")
        if not isinstance(raw_items, list):
            raise ValueError(f"model.response.completed has no conversation items: {event.id}")
        return [CONVERSATION_ADAPTER.validate_python(item) for item in raw_items]
    if event.event_type in TOOL_RESULT_EVENTS:
        raw_result = payload.get("result_item")
        if not isinstance(raw_result, dict):
            raise ValueError(f"{event.event_type} has no result item: {event.id}")
        return [ToolResultItem.model_validate(raw_result)]
    return []
