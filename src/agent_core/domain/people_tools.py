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
