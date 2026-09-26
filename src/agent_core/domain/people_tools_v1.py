"""The people.history 1.0.0 schemas, kept for chats pinned before ADR-0126.

The class names match the originals so the pinned JSON schemas stay identical.
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.people import InteractionParticipant, PeopleValue


class PeopleHistoryArgs(PeopleValue):
    person_id: UUID
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    known_at: AwareDatetime | None = None
    channel: Literal["chat", "email", "sms"] | None = None
    limit: int = Field(default=5, ge=1, le=5)
    cursor: str | None = Field(default=None, max_length=2048)


class HistoryToolItem(PeopleValue):
    id: UUID
    channel: Literal["chat", "email", "sms"]
    attribution: Literal["observed", "owner_reported", "correspondent_reported"]
    direction: Literal["incoming", "outgoing", "reported"]
    summary: str = Field(max_length=400)
    occurred_at: AwareDatetime | None
    participants: list[InteractionParticipant] = Field(max_length=6)
    source_ids: list[UUID] = Field(max_length=4)
    details_truncated: bool


class PeopleHistoryResult(PeopleValue):
    items: list[HistoryToolItem] = Field(max_length=5)
    next_cursor: str | None = Field(max_length=2048)
    coverage: str = Field(max_length=300)
