"""Source-local People proposals; providers never receive database identities."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.people_time import SourceTimezone


class Proposal(BaseModel):
    """Provider output: strict structured output refuses any optional property."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PersonEvidence(Proposal):
    key: str = Field(min_length=1, max_length=64)
    source_event_id: int = Field(gt=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=512)
    display_name: str = Field(min_length=1, max_length=200)
    identifier_kind: Literal["name", "role", "email", "phone", "handle"]
    identifier_value: str = Field(min_length=1, max_length=512)
    namespace: str = Field(min_length=1, max_length=100)
    context: str = Field(max_length=200)
    role: Literal["subject", "object", "speaker", "mentioned"]
    referent_key: str | None

    @model_validator(mode="after")
    def exact_span_shape(self) -> "PersonEvidence":
        if self.key == "owner":
            raise ValueError("owner is a reserved evidence key")
        if self.end - self.start != len(self.text):
            raise ValueError("People evidence offsets must match its exact text")
        return self


RelationshipPredicate = Literal[
    "parent",
    "child",
    "sibling",
    "relative",
    "partner",
    "spouse",
    "friend",
    "colleague",
    "collaborator",
    "introduced_by",
    "reports_to",
    "employment",
    "founder",
    "board_member",
    "investor",
    "other",
]


class RelationshipProposal(Proposal):
    subject_key: str = Field(min_length=1, max_length=64)
    object_key: str = Field(min_length=1, max_length=64)
    predicate: RelationshipPredicate
    qualifier: str = Field(max_length=200)
    valid_from: AwareDatetime | None
    valid_to: AwareDatetime | None
    precision: Literal["instant", "day", "month", "year", "unknown"]
    source_timezone: SourceTimezone


class CommitmentProposal(Proposal):
    debtor_key: str = Field(min_length=1, max_length=64)
    beneficiary_key: str = Field(min_length=1, max_length=64)
    state: Literal["proposed", "open", "completed", "cancelled", "uncertain"]
    source_event_id: int = Field(gt=0)
    due_at: AwareDatetime | None
    due_precision: Literal["instant", "day", "month", "year", "unknown"]
    source_timezone: SourceTimezone


class OrganizationEvidence(Proposal):
    key: str = Field(min_length=1, max_length=64)
    source_event_id: int = Field(gt=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def exact_span(self) -> "OrganizationEvidence":
        if self.key == "owner":
            raise ValueError("owner is a reserved evidence key")
        if self.end - self.start != len(self.text) or self.display_name not in self.text:
            raise ValueError("organization requires an exact name span")
        return self


class PeopleClaim(Proposal):
    mentions: list[PersonEvidence] = Field(min_length=1, max_length=64)
    organizations: list[OrganizationEvidence] = Field(max_length=32)
    relationship: RelationshipProposal | None
    commitment: CommitmentProposal | None

    @model_validator(mode="after")
    def unique_mentions(self) -> "PeopleClaim":
        keys = {item.key for item in self.mentions} | {item.key for item in self.organizations}
        if len(keys) != len(self.mentions) + len(self.organizations) or len(keys) > 64:
            raise ValueError("source-local mention keys must be unique")
        endpoints = []
        if self.relationship:
            endpoints.extend([self.relationship.subject_key, self.relationship.object_key])
        if self.commitment:
            endpoints.extend([self.commitment.debtor_key, self.commitment.beneficiary_key])
        if not set(endpoints) <= keys | {"owner"}:
            raise ValueError("People endpoint must name local evidence or the owner")
        return self


class InteractionEvidence(Proposal):
    source_event_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=4000)
    summary: str = Field(min_length=1, max_length=1000)
    interaction_kind: Literal[
        "exchange",
        "meeting",
        "visit",
        "introduction",
        "trip",
        "milestone",
        "decision",
        "disagreement",
        "other",
    ]
    occurred_at: AwareDatetime | None
    precision: Literal["instant", "day", "month", "year", "unknown"]
    source_timezone: SourceTimezone
    mentions: list[PersonEvidence] = Field(min_length=1, max_length=64)
    participant_keys: list[str] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def participant_evidence(self) -> "InteractionEvidence":
        keys = {mention.key for mention in self.mentions}
        if len(keys) != len(self.mentions) or not set(self.participant_keys) <= keys:
            raise ValueError("interaction participants require unique local mention evidence")
        if len(set(self.participant_keys)) != len(self.participant_keys):
            raise ValueError("interaction participant keys must be unique")
        if any(mention.source_event_id != self.source_event_id for mention in self.mentions):
            raise ValueError("interaction mentions must belong to its source")
        if self.summary not in self.text:
            raise ValueError("interaction summary must preserve an exact evidence excerpt")
        return self
