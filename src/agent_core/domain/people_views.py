"""Closed People management requests and bounded public projections."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    PeopleCommitment,
    PeopleEndpoint,
    PeopleInteraction,
    PeopleValue,
    Person,
    PersonIdentifier,
    RelationshipAssertion,
    RelationshipPredicate,
)
from agent_core.domain.people_time import SourceTimezone
from agent_core.domain.views import MemoryView


class CreatePerson(PeopleValue):
    session_id: UUID
    display_name: str = Field(min_length=1, max_length=200)
    sensitivity: Sensitivity = Sensitivity.SENSITIVE


class AddPersonAlias(PeopleValue):
    operation: Literal["add"]
    identifier_kind: Literal["name", "email", "phone", "handle", "role"]
    value: str = Field(min_length=1, max_length=512)
    namespace: str = Field(default="owner", min_length=1, max_length=100)
    context: str = Field(default="owner", max_length=200)
    valid_from: AwareDatetime | None = None


class EndPersonAlias(PeopleValue):
    operation: Literal["end"]
    identifier_id: UUID
    expected_revision: int = Field(ge=1)
    valid_to: AwareDatetime


class UpdatePerson(PeopleValue):
    session_id: UUID
    expected_revision: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    confirm: Literal[True] | None = None
    alias: Annotated[AddPersonAlias | EndPersonAlias, Field(discriminator="operation")] | None = (
        None
    )


class PeoplePage(PeopleValue):
    items: list[Person]
    next_cursor: str | None = None


class LegacyPeopleLinkResult(PeopleValue):
    scanned: int = Field(ge=0)
    linked: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    next_cursor: str | None = None
    coverage: str = (
        "Existing owner beliefs with exact, unambiguous, time-valid subject identities "
        "and retained cited sources. Other memories stay unlinked."
    )


class MergeSuggestionDetail(PeopleValue):
    """A possible duplicate with both identities as the owner sees them (ADR-0125)."""

    id: UUID
    revision: int
    source: Person
    target: Person
    reason: Literal["same_name", "first_name", "nickname", "same_address"]
    family_name: bool
    state: Literal["open", "merged", "separated", "withdrawn"]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class MergeSuggestionPage(PeopleValue):
    items: list[MergeSuggestionDetail]
    next_cursor: str | None = None


class AutomaticMergeDetail(PeopleValue):
    """A merge the system applied, which the owner can undo (ADR-0125)."""

    operation_id: UUID
    revision: int
    merged: Person
    merged_at: AwareDatetime


class PersonProfile(PeopleValue):
    person: Person
    aliases: list[PersonIdentifier] = Field(default_factory=list)
    relationships: list[RelationshipAssertion] = Field(default_factory=list)
    history: list[PeopleInteraction] = Field(default_factory=list)
    commitments: list[PeopleCommitment] = Field(default_factory=list)
    facts: list[MemoryView] = Field(default_factory=list)
    fact_revisions: dict[UUID, int] = Field(default_factory=dict)
    related_labels: dict[UUID, str] = Field(default_factory=dict)
    merge_suggestions: list[MergeSuggestionDetail] = Field(default_factory=list)
    automatic_merges: list[AutomaticMergeDetail] = Field(default_factory=list)
    truncated: bool = False
    coverage: str = (
        "Only recorded, permitted evidence is shown; earlier history may be unavailable."
    )


class PeopleSectionQuery(PeopleValue):
    section: Literal["relationships", "history", "facts", "identity-evidence"]
    as_of: AwareDatetime | None = None
    known_at: AwareDatetime | None = None
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    channel: Literal["chat", "email", "sms"] | None = None
    interaction_kind: (
        Literal[
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
        | None
    ) = None
    unknown_time: Literal["include", "only", "exclude"] = "include"
    include_inactive: bool = False
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)


class IdentityEvidenceView(PeopleValue):
    id: UUID
    revision: int
    kind: Literal[
        "identifier", "mention", "memory_link", "relationship", "interaction", "commitment"
    ]
    label: str
    support_ids: list[UUID]
    belief_id: UUID | None = None
    unresolved: bool = False


class PeopleSectionPage(PeopleValue):
    items: list[RelationshipAssertion | PeopleInteraction | MemoryView | IdentityEvidenceView] = (
        Field(default_factory=list)
    )
    next_cursor: str | None = None
    fact_revisions: dict[UUID, int] = Field(default_factory=dict)
    coverage: str = "Recorded evidence only; earlier history may be unavailable."


class PeopleEvidenceView(PeopleValue):
    reference: UUID
    source_kind: Literal["owner", "email", "sms"]
    session_id: UUID
    event_sequence: int
    evidence_at: AwareDatetime
    account_id: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    email_thread_id: UUID | None = None
    owner_assertion: str | None = Field(default=None, max_length=16384)


class PeopleIdentityRequest(PeopleValue):
    session_id: UUID
    operation: Literal["merge", "split", "undo", "apply"]
    source_id: UUID | None = None
    target_id: UUID | None = None
    operation_id: UUID | None = None
    expected_revision: int = Field(default=1, ge=1)
    expected_revisions: dict[UUID, int] = Field(default_factory=dict, max_length=20)
    selected_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class RelationshipReplacement(PeopleValue):
    kind: Literal["relationship"]
    id: UUID
    expected_revision: int = Field(ge=1)
    subject: PeopleEndpoint
    object: PeopleEndpoint
    predicate: RelationshipPredicate
    qualifier: str = Field(default="", max_length=200)
    precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    source_timezone: SourceTimezone = None


class CommitmentReplacement(PeopleValue):
    kind: Literal["commitment"]
    id: UUID
    expected_revision: int = Field(ge=1)
    debtor: PeopleEndpoint
    beneficiary: PeopleEndpoint
    description: str = Field(min_length=1, max_length=1000)
    state: Literal["proposed", "open", "completed", "cancelled", "uncertain"]
    due_at: AwareDatetime | None = None
    due_precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    source_timezone: SourceTimezone = None


class PeopleCorrectionRequest(PeopleValue):
    session_id: UUID
    belief_id: UUID
    expected_revision: int = Field(ge=1)
    expected_position: int = Field(ge=1)
    operation: Literal["correct", "changed", "reject", "affirm", "remove"]
    effective_at: AwareDatetime | None = None
    projection: (
        Annotated[RelationshipReplacement | CommitmentReplacement, Field(discriminator="kind")]
        | None
    ) = None
    statement: str | None = Field(default=None, min_length=1, max_length=8192)


class PeopleForgetRequest(PeopleValue):
    session_id: UUID
    phase: Literal["preview", "apply"]
    expected_revision: int = Field(ge=1)
    operation_id: UUID | None = None


class PeopleErasureView(PeopleValue):
    id: UUID
    revision: int
    state: Literal["preview", "cleanup_pending", "completed"]
    counts: dict[str, int]
    scope: str = "Derived People memories and history; original messages remain in their source."


class PeopleCorrectionResult(PeopleValue):
    person_revision: int
    belief: MemoryView | None = None
    removed: bool = False
    erasure: PeopleErasureView | None = None


class PeopleRepairCandidate(PeopleValue):
    """A person the directory repair removes, and why (ADR-0121)."""

    person_id: UUID
    display_name: str
    reason: Literal["unconfirmed", "pronoun", "self", "group"]


class PeopleRepairReport(PeopleValue):
    """What `agent people repair-directory` found or did (ADR-0121).

    A preview lists who would be removed before the correspondence backfill,
    which can only keep more people. A confirmed run lists who was removed.
    """

    confirmed: bool
    retained_mail: int = Field(ge=0)
    mail_projected: int = Field(ge=0)
    mail_skipped: int = Field(ge=0)
    aliases_added: list[str]
    candidates: list[PeopleRepairCandidate]
    beliefs_deleted: int = Field(ge=0)
    beliefs_unlinked: int = Field(ge=0)
    # Deleting a fact resets the generated summary of the mail thread it came from.
    mail_threads_reset: int = Field(ge=0)
    note: str


class ResolveMergeSuggestion(PeopleValue):
    """The owner's answer to a possible duplicate (ADR-0125)."""

    session_id: UUID
    expected_revision: int = Field(ge=1)
    decision: Literal["merge", "separate"]


class PeopleMergeEntry(PeopleValue):
    """One merge applied or proposed by the duplicate pass."""

    source_id: UUID
    target_id: UUID
    source_name: str
    target_name: str
    reason: Literal["same_name", "first_name", "nickname", "same_address"]
    family_name: bool = False


class PeopleDedupeReport(PeopleValue):
    """What one duplicate pass merged, or would merge, and what it asks the owner."""

    applied: bool
    merges: list[PeopleMergeEntry]
    suggestions: list[PeopleMergeEntry]
    withdrawn: int = Field(ge=0)
