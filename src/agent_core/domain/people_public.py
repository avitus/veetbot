"""Explicit HTTP projections; persistence metadata never becomes a public contract."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import InteractionParticipant, PeopleEndpoint
from agent_core.domain.people_time import SourceTimezone
from agent_core.domain.people_views import IdentityEvidenceView, PeopleErasureView
from agent_core.domain.views import MemoryView


class Projection(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True, extra="forbid")


class EntityView(Projection):
    id: UUID
    revision: int
    sensitivity: Sensitivity
    support_ids: list[UUID]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PersonView(EntityView):
    kind: Literal["person"]
    display_name: str
    state: Literal["provisional", "active", "merged"]
    pinned: bool
    merged_into: UUID | None


class IdentifierView(EntityView):
    kind: Literal["identifier"]
    person_id: UUID | None
    identifier_kind: Literal["name", "email", "phone", "handle", "role"]
    namespace: str
    value: str
    context: str
    verification: Literal["owner_confirmed", "channel_observed", "contextual"]
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None


class RelationshipView(EntityView):
    kind: Literal["relationship"]
    subject: PeopleEndpoint
    object: PeopleEndpoint
    predicate: str
    qualifier: str
    belief_id: UUID
    valid_from: AwareDatetime | None
    valid_to: AwareDatetime | None
    precision: Literal["instant", "day", "month", "year", "unknown"]
    source_timezone: SourceTimezone = None
    unresolved: bool


class InteractionView(EntityView):
    kind: Literal["interaction"]
    channel: Literal["chat", "email", "sms"]
    interaction_kind: str
    attribution: Literal["observed", "owner_reported", "correspondent_reported"]
    direction: Literal["incoming", "outgoing", "reported"]
    summary: str
    occurred_at: AwareDatetime | None
    ended_at: AwareDatetime | None
    precision: Literal["instant", "day", "month", "year", "unknown"]
    source_timezone: SourceTimezone = None
    participants: list[InteractionParticipant]


class CommitmentView(EntityView):
    kind: Literal["commitment"]
    debtor: PeopleEndpoint
    beneficiary: PeopleEndpoint
    description: str
    state: Literal["proposed", "open", "completed", "cancelled", "uncertain"]
    belief_id: UUID
    due_at: AwareDatetime | None
    due_precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    source_timezone: SourceTimezone = None
    unresolved: bool


class PersonProfileView(Projection):
    person: PersonView
    aliases: list[IdentifierView]
    relationships: list[RelationshipView]
    history: list[InteractionView]
    commitments: list[CommitmentView]
    facts: list[MemoryView]
    fact_revisions: dict[UUID, int]
    related_labels: dict[UUID, str]
    truncated: bool
    coverage: str


class PeoplePageView(Projection):
    items: list[PersonView]
    next_cursor: str | None


class PeopleSectionPageView(Projection):
    items: list[RelationshipView | InteractionView | MemoryView | IdentityEvidenceView]
    next_cursor: str | None
    fact_revisions: dict[UUID, int] = Field(default_factory=dict)
    coverage: str


class AssignmentView(Projection):
    entity_id: UUID
    before_person_id: UUID
    after_person_id: UUID
    expected_revision: int
    previous_unresolved: bool | None
    replacement_unresolved: bool | None


class IdentityOperationView(Projection):
    id: UUID
    revision: int
    operation: Literal["merge", "split", "undo", "forget"]
    state: Literal["preview", "completed", "cleanup_pending", "cancelled"]
    person_ids: list[UUID]
    expected_revisions: dict[UUID, int]
    assignments: list[AssignmentView]
    expires_at: AwareDatetime
    undo_of: UUID | None


OperationView = IdentityOperationView | PeopleErasureView
