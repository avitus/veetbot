"""Source-backed, revisioned People values (Milestone 28)."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from agent_core.domain.memory import Sensitivity
from agent_core.domain.people_imports import (
    ImportState,
    PeopleImportRetry,
    PeopleImportScope,
    PeopleMailboxProgress,
)
from agent_core.domain.people_time import SourceTimezone

PEOPLE_SCHEMA_VERSION = "people-schema@1"
PEOPLE_RESOLVER_VERSION = "people-resolver@1"


class PeopleValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PeopleEntity(PeopleValue):
    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    sensitivity: Sensitivity = Sensitivity.SENSITIVE
    support_ids: list[UUID] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def valid_revision(self) -> PeopleEntity:
        if self.updated_at < self.created_at:
            raise ValueError("record update precedes creation")
        if len(set(self.support_ids)) != len(self.support_ids):
            raise ValueError("source support must be unique")
        return self


class Person(PeopleEntity):
    kind: Literal["person"] = "person"
    display_name: str = Field(min_length=1, max_length=200)
    state: Literal["provisional", "active", "merged"] = "provisional"
    pinned: bool = False
    merged_into: UUID | None = None

    @model_validator(mode="after")
    def merge_consistent(self) -> Person:
        if (self.state == "merged") != (self.merged_into is not None):
            raise ValueError("merged identity requires a redirect")
        if self.merged_into == self.id:
            raise ValueError("identity cannot redirect to itself")
        return self


class PersonIdentifier(PeopleEntity):
    kind: Literal["identifier"] = "identifier"
    person_id: UUID | None = None
    identifier_kind: Literal["name", "email", "phone", "handle", "role"]
    namespace: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=512)
    context: str = Field(default="", max_length=200)
    verification: Literal["owner_confirmed", "channel_observed", "contextual"]
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_assignment(self) -> PersonIdentifier:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("identifier assignment interval must be positive")
        return self


class PeopleSource(PeopleEntity):
    kind: Literal["source"] = "source"
    session_id: UUID
    event_sequence: int = Field(ge=1)
    source_kind: Literal["owner", "email", "sms"]
    evidence_at: AwareDatetime
    account_id: str | None = Field(default=None, max_length=200)
    thread_id: str | None = Field(default=None, max_length=512)
    message_id: str | None = Field(default=None, max_length=512)
    source_revision: str = Field(min_length=1, max_length=128)
    copy_group: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    excluded: bool = False

    @model_validator(mode="after")
    def complete_source(self) -> PeopleSource:
        if self.source_kind == "email" and not all(
            (self.account_id, self.thread_id, self.message_id)
        ):
            raise ValueError("email provenance must be account qualified")
        return self


class PersonMention(PeopleEntity):
    kind: Literal["mention"] = "mention"
    source_id: UUID
    person_id: UUID | None = None
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    role: Literal["subject", "object", "speaker", "mentioned"] = "mentioned"
    resolver_version: str = PEOPLE_RESOLVER_VERSION

    @model_validator(mode="after")
    def positive_span(self) -> PersonMention:
        if self.end <= self.start:
            raise ValueError("mention span must be nonempty")
        if self.source_id not in self.support_ids:
            raise ValueError("mention requires its exact source support")
        return self


class PersonMemoryLink(PeopleEntity):
    unresolved: bool = False
    kind: Literal["memory_link"] = "memory_link"
    person_id: UUID
    belief_id: UUID
    role: Literal["subject", "object", "speaker", "mentioned"] = "subject"


class PeopleEndpoint(PeopleValue):
    kind: Literal["owner", "person", "organization"]
    id: UUID | None = None

    @model_validator(mode="after")
    def typed_reference(self) -> PeopleEndpoint:
        if (self.kind == "owner") != (self.id is None):
            raise ValueError("owner is implicit; other endpoints require an identifier")
        return self


class OrganizationReference(PeopleEntity):
    kind: Literal["organization"] = "organization"
    display_name: str = Field(min_length=1, max_length=200)


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


PeopleRelationshipFilter = Literal["family", "partner", "friend", "work", "other"]
OWNER_RELATIONSHIP_GROUPS: dict[str, tuple[str, ...]] = {
    "family": ("parent", "child", "sibling", "relative"),
    "partner": ("partner", "spouse"),
    "friend": ("friend",),
    "work": (
        "colleague",
        "collaborator",
        "reports_to",
        "employment",
        "founder",
        "board_member",
        "investor",
    ),
    "other": ("introduced_by", "other"),
}


class RelationshipAssertion(PeopleEntity):
    unresolved: bool = False
    kind: Literal["relationship"] = "relationship"
    subject: PeopleEndpoint
    object: PeopleEndpoint
    predicate: RelationshipPredicate
    qualifier: str = Field(default="", max_length=200)
    belief_id: UUID
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    source_timezone: SourceTimezone = None

    @model_validator(mode="after")
    def relationship_interval(self) -> RelationshipAssertion:
        if any(
            endpoint.kind == "organization" for endpoint in (self.subject, self.object)
        ) and self.predicate in {
            "parent",
            "child",
            "sibling",
            "relative",
            "partner",
            "spouse",
            "friend",
        }:
            raise ValueError("personal relationship requires people or owner endpoints")
        if self.subject == self.object:
            raise ValueError("relationship endpoints must differ")
        if (
            self.valid_to is not None
            and self.valid_from is not None
            and self.valid_to <= self.valid_from
        ):
            raise ValueError("relationship interval must be positive")
        return self


class InteractionParticipant(PeopleValue):
    person_id: UUID
    role: Literal["sender", "recipient", "participant", "mentioned"]


class PeopleInteraction(PeopleEntity):
    kind: Literal["interaction"] = "interaction"
    superseded_by: UUID | None = None
    channel: Literal["chat", "email", "sms"]
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
    attribution: Literal["observed", "owner_reported", "correspondent_reported"]
    direction: Literal["incoming", "outgoing", "reported"]
    summary: str = Field(min_length=1, max_length=1000)
    occurred_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    source_timezone: SourceTimezone = None
    participants: list[InteractionParticipant] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def interaction_consistent(self) -> PeopleInteraction:
        if self.superseded_by is not None and (
            self.superseded_by == self.id
            or self.channel != "email"
            or self.interaction_kind != "exchange"
            or self.attribution != "observed"
        ):
            raise ValueError("only observed email copies may redirect to another interaction")
        keys = [(p.person_id, p.role) for p in self.participants]
        if len(keys) != len(set(keys)):
            raise ValueError("interaction participant roles must be unique")
        if self.ended_at and (not self.occurred_at or self.ended_at < self.occurred_at):
            raise ValueError("interaction interval is reversed")
        return self


class PeopleCommitment(PeopleEntity):
    unresolved: bool = False
    kind: Literal["commitment"] = "commitment"
    debtor: PeopleEndpoint
    beneficiary: PeopleEndpoint
    description: str = Field(min_length=1, max_length=1000)
    state: Literal["proposed", "open", "completed", "cancelled", "uncertain"]
    belief_id: UUID
    due_at: AwareDatetime | None = None
    due_precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    source_timezone: SourceTimezone = None
    state_source_id: UUID
    interaction_ids: list[UUID] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def commitment_support(self) -> PeopleCommitment:
        if self.state_source_id not in self.support_ids:
            raise ValueError("commitment state requires its source support")
        return self


class IdentityAssignment(PeopleValue):
    previous_unresolved: bool | None = None
    replacement_unresolved: bool | None = None
    entity_id: UUID
    before_person_id: UUID
    after_person_id: UUID
    expected_revision: int = Field(ge=1)
    previous_participants: list[InteractionParticipant] | None = Field(default=None, max_length=64)
    replacement_participants: list[InteractionParticipant] | None = Field(
        default=None, max_length=64
    )


class PeopleOperation(PeopleEntity):
    kind: Literal["operation"] = "operation"
    operation: Literal["merge", "split", "undo", "forget"]
    state: Literal["preview", "completed", "cleanup_pending", "cancelled"]
    person_ids: list[UUID] = Field(min_length=1, max_length=20)
    expected_revisions: dict[UUID, int] = Field(default_factory=dict)
    assignments: list[IdentityAssignment] = Field(default_factory=list, max_length=1000)
    expires_at: AwareDatetime
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    assignment_scope_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    undo_of: UUID | None = None
    original_states: dict[UUID, Literal["active", "provisional"]] = Field(default_factory=dict)


class PeopleErasure(PeopleEntity):
    kind: Literal["erasure"] = "erasure"
    state: Literal["preview", "cleanup_pending", "completed"] = "preview"
    target_id: UUID
    expected_revisions: dict[UUID, int] = Field(max_length=2000)
    belief_positions: dict[UUID, int] = Field(default_factory=dict, max_length=1000)
    source_sessions: list[UUID] = Field(default_factory=list, max_length=1000)
    pending_artifact_ids: list[UUID] = Field(default_factory=list, max_length=3000)
    pending_people: bool = False
    pending_generated: bool = False
    pending_email: bool = False
    pending_episodes: bool = False
    pending_belief_ids: list[UUID] = Field(default_factory=list, max_length=256)
    pending_copy_ids: list[UUID] = Field(default_factory=list, max_length=3000)
    pending_run_ids: list[UUID] = Field(default_factory=list, max_length=3000)
    blocked_source_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    blocked_record_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    blocked_belief_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    expires_at: AwareDatetime
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    counts: dict[str, int] = Field(default_factory=dict)
    # Internal bounded pages; only the root is a public receipt. Child IDs are
    # deterministic so a large graph does not require an unbounded ID array.
    parent_id: UUID | None = None
    batch_count: int = Field(default=0, ge=0)
    cleanup_after_batch: int = Field(default=0, ge=0)
    cleanup_remaining: int = Field(default=0, ge=0)


class PeopleImportJob(PeopleEntity):
    kind: Literal["import_job"] = "import_job"
    state: ImportState = "preview"
    scope: PeopleImportScope
    audit_session_id: UUID
    worker_session_id: UUID | None = None
    run_id: UUID | None = None
    identity_revisions: dict[UUID, int] = Field(default_factory=dict, max_length=303)
    account_servers: dict[str, dict[str, str]] = Field(default_factory=dict, max_length=10)
    mailbox: PeopleMailboxProgress = Field(default_factory=PeopleMailboxProgress)
    event_after_at: AwareDatetime | None = None
    event_after_id: int | None = Field(default=None, ge=1)
    email_after_at: AwareDatetime | None = None
    email_after_key: str | None = Field(default=None, max_length=64)
    email_current_key: str | None = Field(default=None, max_length=64)
    email_passage_offset: int | None = Field(default=None, ge=0)
    email_current_processed: bool = False
    # The passage after the cursor was rejected and is counted in `failures`.
    email_current_failed: bool = False
    session_cursors: dict[UUID, int] = Field(default_factory=dict, max_length=100)
    email_cursors: dict[str, str | None] = Field(default_factory=dict, max_length=10)
    finished_sessions: list[UUID] = Field(default_factory=list, max_length=100)
    finished_accounts: list[str] = Field(default_factory=list, max_length=10)
    records_read: int = Field(default=0, ge=0)
    records_processed: int = Field(default=0, ge=0)
    records_excluded: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    retry_source: PeopleImportRetry | None = None
    spent_usd: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)
    reservations: dict[UUID, Decimal] = Field(default_factory=dict, max_length=1000)
    source_read_complete: bool = False
    analysis_complete: bool = False
    known_records: int | None = Field(default=None, ge=0)
    coverage: str = "Selected retained sources only; unavailable earlier history is not covered."
    error_code: str | None = Field(default=None, max_length=100)
    expires_at: AwareDatetime
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: Literal["formation@11"] = "formation@11"
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bounded_accounting(self) -> PeopleImportJob:
        if any(not value.is_finite() or value < 0 for value in self.reservations.values()):
            raise ValueError("import reservations must be finite nonnegative amounts")
        if (
            self.state != "failed"
            and self.spent_usd + sum(self.reservations.values()) > self.scope.max_cost_usd
        ):
            raise ValueError("import costs and reservations exceed the approved cap")
        return self


PeopleRecord = Annotated[
    Person
    | PersonIdentifier
    | PeopleSource
    | PersonMention
    | PersonMemoryLink
    | OrganizationReference
    | RelationshipAssertion
    | PeopleInteraction
    | PeopleCommitment
    | PeopleOperation
    | PeopleErasure
    | PeopleImportJob,
    Field(discriminator="kind"),
]
PEOPLE_RECORD: TypeAdapter[PeopleRecord] = TypeAdapter(PeopleRecord)


class PeopleQuery(PeopleValue):
    tenant_id: str
    principal_id: str
    kinds: list[str] = Field(default_factory=lambda: ["person"], max_length=10)
    sensitivity_ceiling: Sensitivity
    states: list[str] | None = Field(default=None, min_length=1, max_length=10)
    pinned: bool | None = None
    relationship: PeopleRelationshipFilter | None = None
    person_id: UUID | None = None
    belief_id: UUID | None = None
    belief_ids: list[UUID] | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, max_length=512)
    root_erasures_only: bool = False
    include_superseded: bool = False
    search_aliases: bool = False
    mentioned_in: str | None = Field(default=None, max_length=8192)
    source_id: UUID | None = None
    session_id: UUID | None = None
    account_id: str | None = None
    thread_id: str | None = None
    message_ids: list[str] = Field(default_factory=list, max_length=1000)
    known_at: AwareDatetime | None = None
    as_of: AwareDatetime | None = None
    channel: Literal["chat", "email", "sms"] | None = None
    interaction_kind: str | None = Field(default=None, max_length=32)
    unknown_time: Literal["include", "only", "exclude"] = "include"
    sort: Literal["id", "history", "recent"] = "id"
    after_event_at: AwareDatetime | None = None
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    after: UUID | None = None
    limit: int = Field(default=50, ge=1, le=100)
    # ADR-0121 identity lookup: an exact (case-insensitive) identifier or name
    # value, whether identifiers must be attached to a person, the instant an
    # identifier must be valid at, and one row per distinct assignment so
    # per-message copies of one address cannot crowd out a match.
    identifier_value: str | None = Field(default=None, min_length=1, max_length=512)
    assigned: Literal["any", "attached", "unattached"] = "any"
    valid_at: AwareDatetime | None = None
    distinct_assignments: bool = False
    # ADR-0121 review queue: provisional, unpinned people with no attached
    # owner-confirmed or channel-observed identifier.
    needs_review: bool = False

    @model_validator(mode="after")
    def ordered_range(self) -> PeopleQuery:
        if self.since and self.until and self.since >= self.until:
            raise ValueError("history interval must be positive")
        if self.distinct_assignments and self.sort != "id":
            raise ValueError("distinct assignments are listed in identifier order")
        return self


# Words that refer to a speaker, a listener, or nobody in particular. A mention
# carrying one of these as its whole label is never a person of its own
# (ADR-0121): pronouns bind to source participants or stay unresolved.
NON_PERSON_REFERENCES: frozenset[str] = frozenset(
    {
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "ourselves",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "he",
        "him",
        "his",
        "himself",
        "she",
        "her",
        "hers",
        "herself",
        "they",
        "them",
        "their",
        "theirs",
        "themselves",
        "it",
        "its",
        "itself",
        "someone",
        "somebody",
        "anyone",
        "anybody",
        "everyone",
        "everybody",
        "no one",
        "nobody",
        "user",
        "the user",
        "owner",
        "the owner",
    }
)


def is_self_reference(kind: str, value: str, refs: frozenset[str]) -> bool:
    """Whether a mention names the owner by one of the owner's addresses or handles."""
    cleaned = value.strip().casefold()
    if kind == "handle":
        return "handle:" + cleaned.lstrip("@") in refs
    return "email:" + cleaned in refs


def is_non_person_reference(label: str) -> bool:
    """Whether a mention label is a pronoun or generic reference, never a person."""
    return " ".join(label.casefold().split()).strip(".,;:!?'\"") in NON_PERSON_REFERENCES


# Whole words that make a label name a group, a department, an organization,
# or an automated service rather than one person (ADR-0125). Singular roles such
# as "partner", "analyst", or "manager" stay person references, and words that
# are also common surnames (for example "Mailer" or "Bank") are left out.
GROUP_OR_SERVICE_WORDS: frozenset[str] = frozenset(
    {
        "accounting",
        "accounts",
        "admin",
        "admins",
        "administration",
        "admissions",
        "alerts",
        "api",
        "association",
        "billing",
        "board",
        "bookings",
        "bot",
        "capital",
        "careers",
        "clients",
        "clinic",
        "college",
        "committee",
        "community",
        "company",
        "compliance",
        "corp",
        "corporation",
        "council",
        "crew",
        "customer",
        "customers",
        "daemon",
        "department",
        "dept",
        "developers",
        "digest",
        "directors",
        "employees",
        "engineers",
        "events",
        "everyone",
        "executives",
        "faculty",
        "families",
        "family",
        "feedback",
        "finance",
        "foundation",
        "founders",
        "friends",
        "fund",
        "group",
        "groups",
        "help",
        "helpdesk",
        "hospital",
        "hr",
        "inc",
        "info",
        "information",
        "institute",
        "insurance",
        "investors",
        "invoices",
        "labs",
        "leadership",
        "legal",
        "llc",
        "ltd",
        "managers",
        "marketing",
        "media",
        "members",
        "membership",
        "news",
        "newsletter",
        "noreply",
        "notification",
        "notifications",
        "oauth",
        "office",
        "operations",
        "orders",
        "parents",
        "partners",
        "payroll",
        "press",
        "receipts",
        "recruiting",
        "recruitment",
        "relations",
        "reservations",
        "returns",
        "robot",
        "sales",
        "security",
        "service",
        "services",
        "shipping",
        "society",
        "squad",
        "staff",
        "students",
        "studios",
        "support",
        "survey",
        "teachers",
        "team",
        "teams",
        "university",
        "updates",
        "ventures",
        "verification",
        "volunteers",
    }
)


def is_group_or_service_name(label: str) -> bool:
    """Whether a label names a group, a service, or an address rather than a person."""
    cleaned = unicodedata.normalize("NFC", label).strip()
    if "@" in cleaned:
        # An address shown as a display name identifies no one by name.
        return True
    return any(
        word in GROUP_OR_SERVICE_WORDS for word in re.findall(r"[^\W\d_]+", cleaned.casefold())
    )


def normalize_identifier(kind: str, namespace: str, value: str) -> str:
    """Only normalization justified without provider-specific alias evidence."""
    if kind not in {"name", "role", "email", "phone", "handle"} or not namespace.strip():
        raise ValueError("identifier kind and namespace are required")
    cleaned = unicodedata.normalize("NFC", value).strip()
    if not cleaned:
        raise ValueError("identifier must not be empty")
    if kind in {"name", "role"}:
        return " ".join(cleaned.casefold().split())
    if kind == "email":
        local, separator, domain = cleaned.rpartition("@")
        if not separator or not local or not domain or any(c.isspace() for c in cleaned):
            raise ValueError("invalid email identifier")
        return local + "@" + domain.casefold()
    if kind == "phone" and (
        not cleaned.startswith("+") or not cleaned[1:].isdigit() or not 8 <= len(cleaned) <= 16
    ):
        raise ValueError("phone identifier requires an explicit international prefix")
    return cleaned


def referenced_people(record: PeopleRecord) -> set[UUID]:
    if isinstance(record, PeopleImportJob):
        return set(record.scope.person_ids)
    if isinstance(record, PeopleOperation):
        return set(record.person_ids)
    if isinstance(record, Person) and record.merged_into:
        return {record.merged_into}
    if isinstance(record, (PersonIdentifier, PersonMention, PersonMemoryLink)):
        return {record.person_id} if record.person_id else set()
    if isinstance(record, PeopleInteraction):
        return {p.person_id for p in record.participants}
    if isinstance(record, RelationshipAssertion):
        return {p.id for p in (record.subject, record.object) if p.id and p.kind == "person"}
    if isinstance(record, PeopleCommitment):
        return {p.id for p in (record.debtor, record.beneficiary) if p.id and p.kind == "person"}
    return set()


def referenced_organizations(record: PeopleRecord) -> set[UUID]:
    if isinstance(record, RelationshipAssertion):
        return {p.id for p in (record.subject, record.object) if p.id and p.kind == "organization"}
    if isinstance(record, PeopleCommitment):
        return {
            p.id for p in (record.debtor, record.beneficiary) if p.id and p.kind == "organization"
        }
    return set()


def event_time(record: PeopleRecord) -> datetime:
    if isinstance(record, PeopleInteraction) and record.occurred_at:
        return record.occurred_at
    return record.created_at


def referenced_assignments(record: PeopleRecord) -> set[UUID]:
    """Operation metadata inherits the privacy and erasure of every affected row."""
    if isinstance(record, PeopleOperation):
        return {a.entity_id for a in record.assignments}
    if isinstance(record, PeopleCommitment):
        return set(record.interaction_ids)
    if isinstance(record, PeopleInteraction) and record.superseded_by is not None:
        return {record.superseded_by}
    return set()


def dependencies(record: PeopleRecord) -> set[UUID]:
    return (
        set(record.support_ids)
        | referenced_people(record)
        | referenced_organizations(record)
        | referenced_assignments(record)
    )


class PeopleCopyCleanup(PeopleValue):
    pending_generated: bool = False
    active_run_ids: list[UUID] = Field(default_factory=list)
    pending_run_ids: list[UUID] = Field(default_factory=list)
    counts: dict[str, int]
    artifact_ids: list[UUID] = Field(max_length=256)
