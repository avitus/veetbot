"""Closed People tool arguments; limits are shared by schema and execution."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.memory import BeliefType, Portability, Sensitivity
from agent_core.domain.people import InteractionParticipant, PeopleValue


class PeopleSearchArgs(PeopleValue):
    text: str = Field(min_length=1, max_length=200)
    kind: Literal["name", "role", "email", "phone", "handle"] = "name"
    namespace: str = Field(default="owner", min_length=1, max_length=100)
    context: str = Field(default="owner", max_length=200)
    at: AwareDatetime | None = None


class PeopleContextArgs(PeopleValue):
    person_ids: list[UUID] = Field(min_length=1, max_length=3)
    text: str = Field(min_length=1, max_length=8192)
    scope: str = Field(min_length=1, max_length=256)
    as_of: AwareDatetime | None = None
    known_at: AwareDatetime | None = None


class PeopleHistoryArgs(PeopleValue):
    person_id: UUID
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    known_at: AwareDatetime | None = None
    channel: Literal["chat", "email", "sms"] | None = None
    limit: int = Field(default=5, ge=1, le=5)
    cursor: str | None = Field(default=None, max_length=2048)
    source_id: UUID | None = Field(
        default=None,
        description=(
            "One of an email item's source_ids: return that message's retained "
            "original text instead of items."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=10_000_000,
        description="Character offset into the original text; use next_offset.",
    )


class PersonReference(PeopleValue):
    person_id: UUID
    expected_revision: int = Field(ge=1)
    role: Literal["subject", "object", "mentioned"] = "subject"


class RememberPeopleArgs(PeopleValue):
    statement: str = Field(min_length=1, max_length=8192)
    subject: str = Field(min_length=1, max_length=512)
    scope: str = Field(min_length=1, max_length=256)
    belief_type: BeliefType = BeliefType.FACT
    portability: Portability | None = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    person_refs: list[PersonReference] = Field(default_factory=list, max_length=6)


class SearchPersonResult(PeopleValue):
    id: UUID
    display_name: str = Field(max_length=200)
    revision: int = Field(ge=1)


class PeopleSearchResult(PeopleValue):
    status: Literal["matched", "ambiguous", "unresolved"]
    candidates: list[SearchPersonResult] = Field(max_length=20)


class PeopleContextResult(PeopleValue):
    trace_id: UUID
    truncated: bool


class HistoryEmailReference(PeopleValue):
    account_id: str = Field(max_length=100)
    message_id: str = Field(max_length=1024)
    # Email mode's cached conversation, for email.context; null once it is gone.
    thread_id: UUID | None


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
    email: HistoryEmailReference | None = None


class PeopleSourceText(PeopleValue):
    source_id: UUID
    account_id: str = Field(max_length=100)
    message_id: str = Field(max_length=1024)
    thread_id: UUID | None
    sender: str = Field(max_length=2000)
    to: str | None = Field(max_length=2000)
    cc: str | None = Field(max_length=2000)
    subject: str = Field(max_length=998)
    sent_at: AwareDatetime
    text: str = Field(max_length=8000)
    offset: int = Field(ge=0)
    next_offset: int | None
    complete: bool


class PeopleHistoryResult(PeopleValue):
    items: list[HistoryToolItem] = Field(max_length=5)
    next_cursor: str | None = Field(max_length=2048)
    coverage: str = Field(max_length=300)
    source: PeopleSourceText | None = None
