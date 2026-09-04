"""Governed long-term memory, recall, and trace values."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
)

from agent_core.domain.events import EventEnvelope, ProcessEvent


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    PROVISIONAL = "provisional"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    RETIRED = "retired"


# A browse that opened onto superseded and retired rows would show history
# rather than belief; a caller that wants history names the statuses it wants.
LIVE_MEMORY_STATUSES: tuple[MemoryStatus, ...] = (MemoryStatus.ACTIVE, MemoryStatus.PROVISIONAL)
LIFECYCLE_POLICY_VERSION = "lifecycle@2"
INTEGRATED_EPISODE_MAX_SUBJECTS = 64
MEMORY_SUBJECT_MAX_LENGTH = 512


class BeliefType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    USER_MODEL_ATTR = "user_model_attr"
    PROCEDURE_POINTER = "procedure_pointer"


class Polarity(StrEnum):
    ASSERT = "assert"
    RETRACT = "retract"


class Portability(StrEnum):
    PORTABLE = "portable"
    CONTEXTUAL = "contextual"
    LOCAL = "local"


class MemoryAuthority(StrEnum):
    USER = "user"
    AFFIRMED = "affirmed"
    INFERRED = "inferred"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


# The only sensitivities a memory may carry when its statement leaves for a provider.
PROVIDER_EGRESS_SENSITIVITIES = frozenset({Sensitivity.PUBLIC, Sensitivity.INTERNAL})


SENSITIVITY_ORDER: dict[Sensitivity, int] = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.RESTRICTED: 3,
}


class RejectionKind(StrEnum):
    UNTRUE = "untrue"
    CHANGED = "changed"
    NOT_HERE = "not_here"
    UNSPECIFIED = "unspecified"
    DELETED = "deleted"


class RecallProfile(StrEnum):
    CORE = "core"
    TASK = "task"
    DEEP = "deep"


class RecallMoment(StrEnum):
    SNAPSHOT = "snapshot"
    IN_TURN = "in_turn"
    CHILD_RUN = "child_run"


class MemoryClaimKind(StrEnum):
    """The closed semantic category a formed claim belongs to."""

    ONGOING_PROJECT = "ongoing_project"
    GOAL = "goal"
    ROLE = "role"
    SKILL = "skill"
    INTEREST = "interest"
    HABIT = "habit"
    CONSTRAINT = "constraint"
    RECURRING_STATE = "recurring_state"
    RELATIONSHIP = "relationship"
    PREFERENCE = "preference"
    RESOURCE = "resource"
    PROJECT_FACT = "project_fact"


class MemoryDerivation(StrEnum):
    """Whether the source states a claim or merely supports an inference."""

    DIRECT = "direct"
    HYPOTHESIS = "hypothesis"


class MemoryLongevity(StrEnum):
    """The evidence horizon the lifecycle applies to a claim."""

    ONGOING = "ongoing"
    DURABLE = "durable"
    TENTATIVE = "tentative"


class EvidenceSpan(BaseModel):
    """One exact source substring supporting an automatic candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_event_id: PositiveInt
    text: str = Field(min_length=1, max_length=8192)


class IntegratedEpisode(BaseModel):
    """A rebuildable, provenance-complete narrative over owned user events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    session_id: UUID
    source_event_ids: list[PositiveInt] = Field(min_length=1, max_length=256)
    source_started_at: datetime
    source_ended_at: datetime
    narrative: str = Field(min_length=1, max_length=32768)
    subjects: list[str] = Field(default_factory=list, max_length=INTEGRATED_EPISODE_MAX_SUBJECTS)
    integration_policy_version: Literal["episode-integration@1"] = "episode-integration@1"
    derivation_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("source_event_ids")
    @classmethod
    def sources_are_ordered_and_unique(cls, value: list[int]) -> list[int]:
        if value != sorted(set(value)):
            raise ValueError("episode source events must be ordered and unique")
        return value

    @field_validator("subjects")
    @classmethod
    def subjects_are_ordered_unique_and_bounded(cls, value: list[str]) -> list[str]:
        normalized = [subject.strip() for subject in value]
        if any(not subject or len(subject) > MEMORY_SUBJECT_MAX_LENGTH for subject in normalized):
            raise ValueError("episode subjects must be non-empty and bounded")
        if len({subject.casefold() for subject in normalized}) != len(normalized):
            raise ValueError("episode subjects must be unique")
        return normalized

    @model_validator(mode="after")
    def source_interval_is_valid(self) -> IntegratedEpisode:
        for stamp in (self.source_started_at, self.source_ended_at, self.created_at):
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("episode timestamps must be timezone-aware")
        if self.source_ended_at < self.source_started_at:
            raise ValueError("episode source interval is reversed")
        return self


class MemoryCandidate(BaseModel):
    """A provenance-bound proposal emitted before policy and conflict gates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    belief_type: BeliefType
    subject: str = Field(min_length=1, max_length=MEMORY_SUBJECT_MAX_LENGTH)
    statement: str = Field(min_length=1, max_length=8192)
    polarity: Polarity = Polarity.ASSERT
    source_event_ids: list[PositiveInt] = Field(min_length=1)
    model_confidence: float = Field(ge=0, le=1)
    proposed_scope: str = Field(min_length=1, max_length=256)
    proposed_portability: Portability
    sensitivity_guess: Sensitivity
    claim_kind: MemoryClaimKind = MemoryClaimKind.PROJECT_FACT
    derivation: MemoryDerivation = MemoryDerivation.DIRECT
    longevity: MemoryLongevity = MemoryLongevity.DURABLE
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list, min_length=1)
    valid_from: datetime | None = None
    expires_hint: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def legacy_candidates_receive_a_provenance_placeholder(cls, value: object) -> object:
        """Keep completed extractors constructible while v9 supplies exact spans.

        Formation@7 and formation@8 predate evidence spans. Their governed
        source check still validates the full statement against the named user
        events. New formation@9 candidates always provide exact substrings.
        """

        if not isinstance(value, dict) or "evidence_spans" in value:
            return value
        source_ids = value.get("source_event_ids")
        statement = value.get("statement")
        if isinstance(source_ids, list) and source_ids and isinstance(statement, str):
            return {
                **value,
                "evidence_spans": [{"source_event_id": source_ids[0], "text": statement}],
            }
        return value

    @model_validator(mode="after")
    def evidence_spans_name_candidate_sources(self) -> MemoryCandidate:
        sources = set(self.source_event_ids)
        if any(span.source_event_id not in sources for span in self.evidence_spans):
            raise ValueError("evidence span must name one of the candidate source events")
        return self


class ProviderExtractionFailure(BaseModel):
    """Safe normalized metadata for a failed provider extraction attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_class: str = Field(min_length=1, max_length=256)
    failure_kind: Literal["transient", "permanent", "protocol"]
    provider_code: str | None = Field(default=None, max_length=128)
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_parameter: str | None = Field(default=None, max_length=128)
    stream_had_output: bool = False
    retryable: bool


class MemoryExtractionResult(list[MemoryCandidate]):
    """List-compatible extractor output with optional content-free failure metadata."""

    def __init__(
        self,
        candidates: Iterable[MemoryCandidate] = (),
        *,
        provider_failure: ProviderExtractionFailure | None = None,
    ) -> None:
        super().__init__(candidates)
        self.provider_failure = provider_failure


def minimum_supported_case_count(positive_case_count: int) -> int:
    """Return the exact eighty-percent positive-coverage floor."""

    return (positive_case_count * 4 + 4) // 5


MINIMUM_LEXICAL_TERM_LENGTH = 3
_TERM_PUNCTUATION = ".,:;!?()[]{}\"'"
# One run of alphanumerics, or several joined by the characters PostgreSQL's
# default parser keeps inside a single token: a host, a path, an address, a
# decimal, a hyphenated word. Underscore separates, as it does there.
_LEXEME_ATOM = re.compile(r"[^\W_]+(?:[-'\u2019./:@][^\W_]+)*")
_LEXEME_APOSTROPHE = re.compile(r"['\u2019]")


def lexical_terms(text: str) -> set[str]:
    """Split text into the terms lexical recall matches on."""

    return {
        stripped
        for part in text.casefold().split()
        if len(stripped := part.strip(_TERM_PUNCTUATION)) >= MINIMUM_LEXICAL_TERM_LENGTH
    }


def lexical_tokens(text: str) -> set[str]:
    """Split text into the lexemes a `simple` full-text vector would hold.

    Both belief stores must answer a text query the same way, and PostgreSQL
    answers it by matching whole lexemes of `to_tsvector('simple', ...)`: it
    lowercases and never stems, so `themes` is not `theme`, it keeps a run
    joined by dots, slashes, colons, or an at sign whole the way a host, path,
    or address stays whole, it splits an apostrophe into its parts, and it
    emits a hyphenated word both whole and in parts.

    The approximation ends at the parser's edges: a URL carrying a query
    string, and a date, are divided differently there. Lexical recall is a
    ranking arm rather than an isolation predicate, so an edge that differs
    changes what the ranker is offered and never what a principal may see.
    """

    tokens: set[str] = set()
    for atom in _LEXEME_ATOM.findall(text.casefold()):
        for part in _LEXEME_APOSTROPHE.split(atom):
            if not part:
                continue
            tokens.add(part)
            if "-" in part:
                tokens.update(piece for piece in part.split("-") if piece)
    return tokens


def lexical_term_lexemes(terms: Iterable[str]) -> list[frozenset[str]]:
    """The lexemes each query term needs, the way `plainto_tsquery` ands them."""

    return [frozenset(lexical_tokens(term)) for term in terms]


def lexical_text_matches(term_lexemes: Iterable[frozenset[str]], text: str) -> bool:
    """Whether any term's lexemes all appear in this text.

    A term that reduces to no lexeme matches nothing, as an empty tsquery does.
    """

    tokens = lexical_tokens(text)
    return any(lexemes and lexemes <= tokens for lexemes in term_lexemes)


def lexical_query_terms(text: str | None) -> list[str]:
    """Order the query's lexical terms so both belief stores match the same set.

    Every store answers a text query with any-term semantics: a record matches
    when it overlaps one term or more. Text too short to yield a term is still
    a query, so it is matched whole rather than matching everything.
    """

    if text is None:
        return []
    terms = sorted(lexical_terms(text))
    if terms:
        return terms
    collapsed = " ".join(text.casefold().split())
    return [collapsed] if collapsed else []


class ProviderExtractionEvaluationEvidence(BaseModel):
    """Version-bound evidence required before provider extraction can activate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    extractor_version: str = Field(min_length=1)
    formation_policy_version: str = Field(min_length=1)
    model_policy: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    policy_profile: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    build_ref: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_count: int = Field(ge=20)
    positive_case_count: int = Field(ge=20)
    minimum_supported_case_count: int = Field(ge=1)
    deterministic_supported_case_count: int = Field(ge=0)
    provider_supported_case_count: int = Field(ge=0)
    deterministic_supported_candidates: int = Field(ge=0)
    provider_supported_candidates: int = Field(ge=0)
    deterministic_fabricated_candidates: int = Field(ge=0)
    provider_fabricated_candidates: int = Field(ge=0)
    deterministic_policy_failures: int = Field(ge=0)
    provider_policy_failures: int = Field(ge=0)
    seeded_case_count: int = Field(default=0, ge=0)
    evaluated_at: datetime

    @model_validator(mode="after")
    def passed_activation_gate(self) -> ProviderExtractionEvaluationEvidence:
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("provider extraction evaluation time must be timezone-aware")
        if self.positive_case_count > self.sample_count:
            raise ValueError("positive case count exceeds the evaluation sample count")
        if self.minimum_supported_case_count > self.positive_case_count:
            raise ValueError("minimum supported case count exceeds positive cases")
        required_coverage = minimum_supported_case_count(self.positive_case_count)
        if self.minimum_supported_case_count != required_coverage:
            raise ValueError("minimum supported case count must equal eighty percent coverage")
        if self.formation_policy_version == "formation@10" and self.seeded_case_count < 1:
            # The repaired policy exists because an empty store hid the frozen
            # one's starvation; its evidence is meaningless without a populated store.
            raise ValueError("formation@10 evidence must come from a populated store")
        if (
            self.deterministic_supported_case_count > self.positive_case_count
            or self.provider_supported_case_count > self.positive_case_count
        ):
            raise ValueError("supported case count exceeds positive cases")
        if self.provider_supported_case_count < self.minimum_supported_case_count:
            raise ValueError("provider extraction evaluation missed the positive coverage floor")
        if self.provider_supported_candidates <= self.deterministic_supported_candidates:
            raise ValueError("provider extraction evaluation did not demonstrate formation lift")
        if self.deterministic_fabricated_candidates or self.provider_fabricated_candidates:
            raise ValueError("memory extraction evaluation observed fabricated candidates")
        if self.provider_policy_failures > self.deterministic_policy_failures:
            raise ValueError("provider extraction evaluation observed a policy regression")
        return self


class MemoryDistillationEvidence(BaseModel):
    """Never-overwritten comparative evidence required to activate formation@9."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    scorer_version: Literal["distillation-scorer@2"]
    extractor_version: Literal["nemori-assisted-v1"] = "nemori-assisted-v1"
    formation_policy_version: Literal["formation@9"] = "formation@9"
    model_policy: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    policy_profile: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    # An immutable commit, never an operator-typed content label.
    build_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_count: int = Field(ge=60)
    positive_case_count: int = Field(ge=42)
    # Every positive case that ran against a populated store; zero would mean the
    # belief view, anticipation, and attributed redundancy were never exercised.
    seeded_case_count: int = Field(ge=1)
    direct_must_form_recall: float = Field(ge=0.95, le=1)
    hypothesis_must_form_recall: float = Field(ge=0.8, le=1)
    benign_precision: float = Field(ge=0.9, le=1)
    useful_recall_lift_percentage_points: float = Field(ge=15, le=100)
    correction_rate_per_hundred: float = Field(ge=0, le=10)
    # The share of gold-evidence clauses the provider formed or represented
    # rather than labelled away as transient, unsafe, or not memory.
    evidence_disposition_precision: float = Field(ge=0.75, le=1)
    # Three batched calls per planned segment, and the measured totals behind
    # that claim: a consolidation over several segments makes three per segment.
    provider_calls_per_segment: Literal[3] = 3
    provider_calls_measured: int = Field(ge=3)
    consolidations_measured: int = Field(ge=1)
    provider_cost_usd: str = Field(pattern=r"^\d+(?:\.\d+)?$")
    boundary_failures: Literal[0] = 0
    comparative_policies: tuple[
        Literal["formation@7"], Literal["formation@8"], Literal["formation@9"]
    ] = ("formation@7", "formation@8", "formation@9")
    evaluated_at: datetime

    @model_validator(mode="after")
    def evidence_thresholds_are_coherent(self) -> MemoryDistillationEvidence:
        if self.positive_case_count > self.sample_count:
            raise ValueError("positive distillation cases exceed the sample")
        if self.positive_case_count * 10 < self.sample_count * 7:
            raise ValueError("distillation evidence is less than seventy percent positive")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("distillation evidence time must be timezone-aware")
        if self.provider_calls_measured % self.provider_calls_per_segment:
            raise ValueError("measured provider calls are not a whole number of segments")
        if self.provider_calls_measured < self.provider_calls_per_segment * (
            self.consolidations_measured
        ):
            raise ValueError("measured provider calls fall short of one segment per consolidation")
        return self


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    scope: str = Field(min_length=1, max_length=256)
    subject: str = Field(min_length=1, max_length=512)
    statement: str = Field(min_length=1, max_length=8192)
    source_session_id: UUID
    source_event_ids: list[PositiveInt] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    sensitivity: Sensitivity
    valid_from: datetime
    expires_at: datetime | None = None
    status: MemoryStatus
    belief_type: BeliefType
    polarity: Polarity = Polarity.ASSERT
    portability: Portability
    origin_scopes: list[str] = Field(min_length=1)
    corroboration_count: PositiveInt = 1
    claim_kind: MemoryClaimKind = MemoryClaimKind.PROJECT_FACT
    derivation: MemoryDerivation = MemoryDerivation.DIRECT
    longevity: MemoryLongevity = MemoryLongevity.DURABLE
    last_evidence_at: datetime = Field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    last_used_at: datetime | None = None
    evidence_count: PositiveInt = 1
    lifecycle_policy_version: str = Field(default="lifecycle@1-backfill", min_length=1)
    last_reinforced_at: datetime
    valid_to: datetime | None = None
    superseded_by: UUID | None = None
    conflicts_with: list[UUID] = Field(default_factory=list)
    flagged_for_review: bool = False
    formation_run_id: UUID
    consolidation_policy_version: str = Field(min_length=1)
    authority: MemoryAuthority
    utility: float = Field(default=0, ge=-1, le=1)
    store_position: PositiveInt
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def backfill_evidence_lifecycle_fields(cls, value: object) -> object:
        """Give pre-lifecycle@2 records conservative, history-preserving values."""

        if not isinstance(value, dict):
            return value
        belief_type = value.get("belief_type")
        raw_type = belief_type.value if isinstance(belief_type, BeliefType) else belief_type
        claim_kind = {
            BeliefType.PREFERENCE.value: MemoryClaimKind.PREFERENCE.value,
            BeliefType.RELATIONSHIP.value: MemoryClaimKind.RELATIONSHIP.value,
            BeliefType.PROCEDURE_POINTER.value: MemoryClaimKind.RESOURCE.value,
        }.get(str(raw_type), MemoryClaimKind.PROJECT_FACT.value)
        source_ids = value.get("source_event_ids")
        evidence_count = len(set(source_ids)) if isinstance(source_ids, list) else 1
        return {
            **value,
            "claim_kind": value.get("claim_kind", claim_kind),
            "derivation": value.get("derivation", MemoryDerivation.DIRECT.value),
            "longevity": value.get("longevity", MemoryLongevity.DURABLE.value),
            "last_evidence_at": value.get("last_evidence_at", value.get("valid_from")),
            "evidence_count": value.get("evidence_count", max(1, evidence_count)),
            "lifecycle_policy_version": value.get(
                "lifecycle_policy_version", "lifecycle@1-backfill"
            ),
        }

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> MemoryRecord:
        if self.status is MemoryStatus.SUPERSEDED and self.valid_to is None:
            raise ValueError("superseded memory requires valid_to")
        if self.superseded_by is not None and self.status is not MemoryStatus.SUPERSEDED:
            raise ValueError("only a superseded memory may identify its replacement")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("memory valid_to precedes valid_from")
        return self


class MemoryBrowseQuery(BaseModel):
    """A principal's own beliefs, paged by store position for a human reader.

    Browsing is a different surface from recall: it returns complete belief
    views for someone paging through everything the store holds, filters
    aside, rather than the ranked, budget-bounded candidates `query()` returns
    for the retrieval arm. `cursor` is the last row's `(store_position, id)`
    the caller has already seen; the store walks strictly past it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    ceiling: Sensitivity
    statuses: tuple[MemoryStatus, ...] = LIVE_MEMORY_STATUSES
    belief_types: tuple[BeliefType, ...] = ()
    subject: str | None = None
    session_id: UUID | None = None
    text: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: tuple[int, UUID] | None = None


class BeliefRejection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str
    principal_id: str
    belief_id: UUID
    kind: RejectionKind
    subject: str
    statement: str | None
    statement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    belief_type: BeliefType
    scope: str
    replacement_id: UUID | None = None
    trace_id: UUID | None = None
    created_at: datetime


class ConsolidationRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str
    principal_id: str
    trigger: str
    scope: str
    session_id: UUID | None = None
    watermark_before: int = Field(ge=0)
    watermark_after: int = Field(ge=0)
    model: str
    policy_version: str
    candidates_proposed: int = Field(ge=0)
    committed: int = Field(ge=0)
    reinforced: int = Field(ge=0)
    superseded: int = Field(ge=0)
    rejected: int = Field(ge=0)
    decision_counts: dict[str, int] = Field(default_factory=dict)
    episode_count: int = Field(default=0, ge=0)
    provider_call_count: int = Field(default=0, ge=0)
    fallback_stages: list[str] = Field(default_factory=list)
    provider_stage_metrics: dict[str, dict[str, int | str | None]] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def decisions_are_nonnegative(self) -> ConsolidationRun:
        if any(count < 0 for count in self.decision_counts.values()):
            raise ValueError("formation decision counts must be nonnegative")
        return self


class ConsolidationResult(BaseModel):
    """What one consolidation produced, beyond the audit row it wrote.

    `conflicted` counts the beliefs committed beside a belief they contradict
    rather than over it. They are already inside the audit's `committed` total;
    the separate count exists because a conflict is the outcome an operator
    most wants to see, and the audit row is column-per-field in the store.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: ConsolidationRun
    beliefs: list[MemoryRecord] = Field(default_factory=list)
    conflicted: int = Field(default=0, ge=0)


class DecayResult(BaseModel):
    """What one decay sweep changed, counted in disjoint outcomes.

    `decayed` counts beliefs that lost a step of confidence and stayed live;
    `retired` counts the ones the step carried below the floor, which are
    closed rather than lowered. A belief is never in both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decayed: int = Field(default=0, ge=0)
    retired: int = Field(default=0, ge=0)


class UsageFeedback(BaseModel):
    """What one completed run's citations fed back into the store.

    `cited` and `uncited` count the beliefs actually written, not mentions: a
    belief returned by several of the run's traces moves once, a belief the
    answer cited is never also counted as unused, and one already at the
    ceiling or the floor is left alone and not counted. `ambiguous` counts
    citations that named more than one returned belief and were therefore
    evidence about none of them. `traces` counts the recall traces the feedback
    read, which is zero when the run holds no recall trace at all and when the
    run's feedback had already been recorded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cited: int = Field(default=0, ge=0)
    uncited: int = Field(default=0, ge=0)
    traces: int = Field(default=0, ge=0)
    ambiguous: int = Field(default=0, ge=0)


class MemoryDiagnosis(BaseModel):
    """Principal-scoped evidence explaining formation state for one session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    watermark: int = Field(ge=0)
    formation_requests: list[EventEnvelope] = Field(default_factory=list)
    provider_selection: ProcessEvent | None = None
    provider_attempts: list[ProcessEvent] = Field(default_factory=list)
    consolidations: list[ConsolidationRun] = Field(default_factory=list)
    beliefs: list[MemoryRecord] = Field(default_factory=list)
    pending_retry: bool = False


class MemoryCorrection(BaseModel):
    """One snapshot belief that stopped holding after the session froze it.

    The frozen prefix goes on rendering the belief it captured, so the
    correction is the only thing that can tell the turn the belief is closed.
    It carries identifiers and an instant rather than statements: the
    successor is already rendered by the delta, and repeating its text here
    would state it twice in two voices.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    belief_id: UUID
    replacement_id: UUID | None = None
    ended_at: datetime

    def render(self) -> str:
        """Render the override line the context builder places in Region B."""

        stamp = self.ended_at.isoformat().replace("+00:00", "Z")
        line = f"correction: [m:{str(self.belief_id)[:8]}] no longer holds as of {stamp}"
        if self.replacement_id is None:
            return f"{line}."
        return f"{line}; superseded by [m:{str(self.replacement_id)[:8]}]."


class RecallQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    principal_id: str
    current_scope: str
    text: str | None = None
    subjects: list[str] = Field(default_factory=list)
    belief_types: list[BeliefType] = Field(default_factory=list)
    as_of: datetime | None = None
    include_superseded: bool = False
    profile: RecallProfile = RecallProfile.TASK
    budget_tokens: PositiveInt
    max_items: PositiveInt
    min_score: float = Field(ge=0, le=1)
    # The recall delta bounds a query by the store position a session froze its
    # snapshot at, so a belief written since is a query result rather than
    # something the caller has to filter for afterwards.
    min_store_position: int = Field(default=0, ge=0)
    sensitivity_ceiling: Sensitivity = Sensitivity.RESTRICTED
    # Beliefs the persona row already carries at higher trust: a hard snapshot
    # exclusion, never a relevance signal (persona-surface.md).
    exclude_ids: tuple[UUID, ...] = ()


class RecalledBelief(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    belief_id: UUID
    subject: str
    statement: str
    belief_type: BeliefType
    claim_kind: MemoryClaimKind = MemoryClaimKind.PROJECT_FACT
    derivation: MemoryDerivation = MemoryDerivation.DIRECT
    longevity: MemoryLongevity = MemoryLongevity.DURABLE
    status: MemoryStatus
    confidence_band: str
    authority: MemoryAuthority
    origin_scope: str
    portability: Portability
    sensitivity: Sensitivity
    carried: bool = False
    valid_from: datetime
    valid_to: datetime | None = None
    score: float
    arms: list[str]
    conflict_with: list[UUID] = Field(default_factory=list)
    blocked: bool = False
    source_event_ids: list[int] = Field(default_factory=list)


class TracedPassage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    document_id: UUID
    title: str
    heading_path: list[str]
    text: str | None
    sensitivity: Sensitivity
    cited: bool = False
    deleted: bool = False


class RecallTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str
    principal_id: str
    session_id: UUID
    run_id: UUID | None = None
    turn_id: UUID | None = None
    moment: RecallMoment
    query: RecallQuery
    surface_id: str
    sensitivity_ceiling: Sensitivity
    rendered: str
    rendered_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm_latencies_ms: dict[str, int] = Field(default_factory=dict)
    candidates: int = Field(ge=0)
    returned: list[UUID] = Field(default_factory=list)
    cited: list[UUID] = Field(default_factory=list)
    dropped_for_budget: list[UUID] = Field(default_factory=list)
    # The operator sweep nulls the identifiers above and leaves this count, so
    # the user-safe projection can still say how many beliefs were considered.
    dropped_for_budget_count: int = Field(default=0, ge=0)
    blocked: list[UUID] = Field(default_factory=list)
    carried_in: list[UUID] = Field(default_factory=list)
    beliefs: list[RecalledBelief] = Field(default_factory=list)
    passages: list[TracedPassage] = Field(default_factory=list)
    retrieval_policy_version: str
    created_at: datetime
    operator_fields_expire_at: datetime

    @property
    def has_operator_fields(self) -> bool:
        """Whether the operator tier still holds anything an expiry would null."""

        return bool(self.arm_latencies_ms or self.candidates or self.dropped_for_budget)

    @property
    def considered_not_shown(self) -> int:
        """Beliefs dropped for budget: by identifier, or by count once expired."""

        return len(self.dropped_for_budget) or self.dropped_for_budget_count


class TracedBelief(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    belief_id: UUID
    subject: str
    statement: str
    learned_at: datetime
    origin_scope: str
    carried: bool
    authority: MemoryAuthority
    source_event_id: int | None
    confidence_band: str
    used: bool


class RecallTraceView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: UUID | None
    moments: list[RecallMoment]
    beliefs: list[TracedBelief]
    passages: list[TracedPassage]
    considered_not_shown: int = Field(ge=0)
    withheld_by_safety: int = Field(ge=0)
    as_of: datetime


class RecallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[RecalledBelief]
    rendered: str
    tokens: int = Field(ge=0)
    truncated: bool
    arms_degraded: list[str] = Field(default_factory=list)
    trace_id: UUID
    watermark: int = Field(default=0, ge=0)


class EpisodeQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    principal_id: str
    session_id: UUID
    text: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: PositiveInt = 50


class MemoryEdit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1, max_length=8192)
    sensitivity: Sensitivity | None = None
    portability: Portability | None = None
    scope: str | None = None
