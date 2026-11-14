"""Recall after a follow-up searches on the whole turn's owner messages (ADR-0324)."""

from __future__ import annotations

from uuid import UUID

from agent_core.context.builder import current_user_text, turn_user_text
from agent_core.domain.events import EventEnvelope, conversation_items
from agent_core.domain.messages import (
    AssistantMessage,
    ConversationItem,
    TextPart,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from tests.contract.support import NOW


def _owner(text: str, *, follow_up: bool = False) -> UserMessage:
    return UserMessage(content=[TextPart(text=text)], follow_up=follow_up)


def test_a_turn_without_follow_ups_searches_exactly_as_before() -> None:
    # An answer to the agent's own question is not a follow-up: the latest
    # owner message alone forms the query, as it always has.
    items: list[ConversationItem] = [
        _owner("Plan the trip to Lisbon."),
        AssistantMessage(content=[TextPart(text="Which month?")]),
        _owner("October."),
    ]
    assert turn_user_text(items) == current_user_text(items) == "October."
    assert turn_user_text([]) is None


def test_follow_ups_join_the_message_they_follow_in_order() -> None:
    items: list[ConversationItem] = [
        _owner("Plan the trip to Lisbon."),
        ToolResultItem(call_id="a", content=[TextPart(text="flights found")]),
        _owner("Keep it under a week.", follow_up=True),
        UserMessage(content=[TextPart(text="Runtime metadata")], trust=TrustLevel.PLATFORM),
        _owner("And my mother is coming.", follow_up=True),
    ]
    assert turn_user_text(items) == (
        "Plan the trip to Lisbon.\nKeep it under a week.\nAnd my mother is coming."
    )
    # A follow-up that only adds a detail no longer hides the request it refines.
    assert current_user_text(items) == "And my mother is coming."


def test_an_answer_after_a_follow_up_keeps_the_request_and_the_order() -> None:
    items: list[ConversationItem] = [
        _owner("Plan the trip to Lisbon."),
        _owner("Keep it under a week.", follow_up=True),
        AssistantMessage(content=[TextPart(text="Which month?")]),
        _owner("October."),
    ]
    assert turn_user_text(items) == "Plan the trip to Lisbon.\nKeep it under a week.\nOctober."


def test_a_read_follow_up_is_marked_when_history_is_rebuilt_from_the_log() -> None:
    def message(payload: dict[str, object]) -> UserMessage:
        (item,) = conversation_items(
            EventEnvelope(
                id=1,
                session_id=UUID(int=2),
                run_id=UUID(int=3),
                sequence=7,
                event_type="user.message.created",
                payload_schema_version=1,
                actor_type="principal",
                actor_id="owner",
                payload=payload,
                trace_id=None,
                created_at=NOW,
            )
        )
        assert isinstance(item, UserMessage)
        return item

    text = [{"kind": "text", "text": "Keep it under a week."}]
    assert message({"content": text, "pending_input_id": str(UUID(int=9))}).follow_up is True
    plain = message({"content": text})
    assert plain.follow_up is False
    # An ordinary message serializes exactly as it did, so stored prefixes keep their bytes.
    assert "follow_up" not in plain.model_dump(mode="json")
