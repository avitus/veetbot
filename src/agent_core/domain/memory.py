"""Governed long-term memory, recall, and trace values."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    PROVISIONAL = "provisional"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    RETIRED = "retired"


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

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> MemoryRecord:
        if self.status is MemoryStatus.SUPERSEDED and self.valid_to is None:
            raise ValueError("superseded memory requires valid_to")
        if self.superseded_by is not None and self.status is not MemoryStatus.SUPERSEDED:
            raise ValueError("only a superseded memory may identify its replacement")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("memory valid_to precedes valid_from")
        return self


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
    started_at: datetime
    finished_at: datetime | None = None


class ConsolidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: ConsolidationRun
    beliefs: list[MemoryRecord] = Field(default_factory=list)


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
    sensitivity_ceiling: Sensitivity = Sensitivity.RESTRICTED


class RecalledBelief(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    belief_id: UUID
    subject: str
    statement: str
    belief_type: BeliefType
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
    blocked: list[UUID] = Field(default_factory=list)
    carried_in: list[UUID] = Field(default_factory=list)
    beliefs: list[RecalledBelief] = Field(default_factory=list)
    passages: list[TracedPassage] = Field(default_factory=list)
    retrieval_policy_version: str
    created_at: datetime
    operator_fields_expire_at: datetime


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
