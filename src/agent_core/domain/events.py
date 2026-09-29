"""Append-only event envelope types."""

from __future__ import annotations

import json
from collections.abc import Sequence
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
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolOutcomeStatus

CONVERSATION_ADAPTER: TypeAdapter[ConversationItem] = TypeAdapter(ConversationItem)
CONTENT_ADAPTER: TypeAdapter[list[ContentPart]] = TypeAdapter(list[ContentPart])
TOOL_RESULT_EVENTS = frozenset(
    {"tool.call.completed", "tool.call.failed", "tool.call.denied", "tool.call.uncertain"}
)
# Version 2 requires the result item every denial feeds the model. Version 1
# carried it only on refusals: an approval or policy denial stored the call's
# name, id and reason code alone, and its upcast result item is None (ADR-0142).
TOOL_CALL_DENIED_PAYLOAD_VERSION = 2
CONVERSATION_MESSAGE_EVENTS = frozenset({"user.message.created", "assistant.message.completed"})
SCHEDULE_INSTRUCTION_EVENT_TYPE = "user.message.created"
SCHEDULE_INSTRUCTION_ACTOR_TYPE = "scheduler"


def validate_event_window(
    session_ids: Sequence[UUID],
    since: datetime,
    until: datetime,
    after: tuple[datetime, int] | None,
    limit: int,
) -> None:
    if (
        not 1 <= len(session_ids) <= 100
        or len(set(session_ids)) != len(session_ids)
        or type(limit) is not int
        or not 1 <= limit <= 256
    ):
        raise ValueError("invalid bounded event window")
    if since.tzinfo is None or until.tzinfo is None or since >= until:
        raise ValueError("event window requires an aware positive date range")
    if after is not None and (after[0].tzinfo is None or after[1] < 1):
        raise ValueError("invalid event window cursor")


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


def is_schedule_instruction_event(event: EventEnvelope) -> bool:
    """Return whether an event is the context-only seed for a scheduled run."""

    return (
        event.event_type == SCHEDULE_INSTRUCTION_EVENT_TYPE
        and event.actor_type == SCHEDULE_INSTRUCTION_ACTOR_TYPE
    )


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
        # A device-ingested message is third-party content the owner did not
        # write. Only that one marker lowers the message's trust; every other
        # value, and its absence, keeps the owner-authored default.
        trust = (
            TrustLevel.EXTERNAL_UNTRUSTED
            if payload.get("trust") == TrustLevel.EXTERNAL_UNTRUSTED.value
            else TrustLevel.USER
        )
        return [
            UserMessage(
                content=parts,
                trust=trust,
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
        if (
            event.event_type == "tool.call.denied"
            and "result_item" in payload
            and raw_result is None
        ):
            return [_unrecorded_denial_result(event)]
        if not isinstance(raw_result, dict):
            raise ValueError(f"{event.event_type} has no result item: {event.id}")
        return [
            ToolResultItem.model_validate(raw_result).model_copy(
                update={"source_event_sequence": event.sequence}
            )
        ]
    return []


def _unrecorded_denial_result(event: EventEnvelope) -> ToolResultItem:
    """Project an upcast version-1 denial that recorded no result item (ADR-0142).

    The provider needs a result for every call it replays, so the denial is
    rendered from what the event recorded and nothing else: its status, the
    tool's name and the reason code. The narration and the tool's output trust
    were never stored, so the item carries no message and the least-trusted
    label instead of a guessed one.
    """

    call_id, name, reason_code = (
        _recorded_text(event, key) for key in ("call_id", "name", "reason_code")
    )
    outcome = {
        "status": ToolOutcomeStatus.DENIED.value,
        "action": name,
        "reason_code": reason_code,
    }
    return ToolResultItem(
        call_id=call_id,
        content=[TextPart(text=json.dumps(outcome, ensure_ascii=False, separators=(",", ":")))],
        is_error=True,
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        source_event_sequence=event.sequence,
    )


def _recorded_text(event: EventEnvelope, key: str) -> str:
    value = event.payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"tool.call.denied has no result item: {event.id}")
    return value
