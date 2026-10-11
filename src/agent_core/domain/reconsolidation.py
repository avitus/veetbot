"""Bounded, owner-scoped reconsolidation maintenance values (M32)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import (
    LIVE_MEMORY_STATUSES,
    PROVIDER_EGRESS_SENSITIVITIES,
    MemoryDerivation,
    MemoryRecord,
    Portability,
    lexical_query_terms,
    lexical_term_lexemes,
    lexical_text_matches,
)

POLICY: Literal["reconsolidation@1"] = "reconsolidation@1"
SLICE_USD = Decimal("0.25")
DAY_USD = Decimal("2")
CONTENT_EXCLUDES = {"utility", "last_used_at", "store_position", "updated_at"}


def content_signature(record: MemoryRecord) -> str:
    payload = record.model_dump(mode="json", exclude=CONTENT_EXCLUDES)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ReconValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceVersion(ReconValue):
    belief_id: UUID
    content_revision: int = Field(ge=1)
    creation_sequence: int = Field(ge=1)


class SourceChange(ReconValue):
    sequence: int = Field(ge=1)
    source: SourceVersion
    reason: Literal["created", "changed", "erased"]


class ReconsolidationJob(ReconValue):
    id: UUID
    tenant_id: str
    principal_id: str
    policy: Literal["reconsolidation@1"] = POLICY
    due_day: date
    generation: int = Field(ge=1)
    full_bound: int = Field(ge=0)
    change_bound: int = Field(ge=0)
    full_cursor: int = Field(default=0, ge=0)
    change_cursor: int = Field(default=0, ge=0)
    lease_owner: str
    lease_token: UUID
    lease_expires_at: AwareDatetime
    slice_started_at: AwareDatetime
    slice_day: date
    slice_spent: Decimal = Field(default=Decimal(0), ge=0)
    requests: int = Field(default=0, ge=0, le=2)
    claimed_groups: int = Field(default=0, ge=0, le=4)
    operations: int = Field(default=0, ge=0, le=8)
    lease_expirations: int = Field(default=0, ge=0)
    state: Literal["running", "ready", "complete"] = "running"
    revision: int = Field(default=1, ge=1)


class InventoryPage(ReconValue):
    sources: tuple[SourceVersion, ...] = ()
    changes: tuple[SourceChange, ...] = ()
    full_cursor: int = Field(ge=0)
    change_cursor: int = Field(ge=0)
    inspected: int = Field(ge=0, le=128)
    excluded: int = Field(ge=0, le=128)


class ReconsolidationGroup(ReconValue):
    id: UUID
    job_id: UUID
    tenant_id: str
    principal_id: str
    policy: Literal["reconsolidation@1"] = POLICY
    sources: tuple[SourceVersion, ...] = Field(min_length=2, max_length=32)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime
    state: Literal["pending", "claimed", "no_change", "failed", "stale", "committed"] = "pending"
    attempts: int = Field(default=0, ge=0, le=3)
    lease_token: UUID | None = None
    reason: Literal[
        "selected",
        "no_change",
        "retry",
        "attempts_exhausted",
        "source_changed",
        "equivalent",
        "summarized",
        "inferred",
        "conflict_flagged",
    ] = "selected"


class CallAdmission(ReconValue):
    batch_id: UUID
    stage: Literal["proposal", "verification"]
    group_ids: tuple[UUID, ...] = Field(min_length=1, max_length=4)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    pricing_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    egress_policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    admitted_at: AwareDatetime

    @model_validator(mode="after")
    def distinct_groups(self) -> CallAdmission:
        if len(set(self.group_ids)) != len(self.group_ids):
            raise ValueError("audit groups must be distinct")
        return self


class CallTokens(ReconValue):
    input_tokens: int = Field(gt=0, le=16384)
    output_tokens: int = Field(gt=0, le=4096)
    cached_input_tokens: int = Field(ge=0, le=16384)
    cache_write_input_tokens: int = Field(ge=0, le=16384)
    cache_write_1h_input_tokens: int = Field(ge=0, le=16384)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class CallCompletion(ReconValue):
    reason: Literal[
        "completed",
        "unavailable",
        "admission_withdrawn",
        "lease_lost",
        "timeout",
        "invalid_response",
        "cancelled",
        "recovered_unknown",
        "unreported",
    ]
    finished_at: AwareDatetime
    elapsed_ms: int | None = Field(default=None, ge=0)
    tokens: CallTokens | None = None


class StageDecision(ReconValue):
    status: Literal["prepared", "reviewed", "deferred", "invalid_response"]
    validated: int = Field(default=0, ge=0, le=8)
    rejected: int = Field(default=0, ge=0, le=8)
    deferred_groups: int = Field(default=0, ge=0, le=4)

    @model_validator(mode="after")
    def bounded_counts(self) -> StageDecision:
        if self.validated + self.rejected > 8:
            raise ValueError("audit candidate counts exceed the batch bound")
        if self.status == "invalid_response" and (
            self.validated or self.rejected or self.deferred_groups
        ):
            raise ValueError("invalid envelopes cannot supply candidate counts")
        return self


class CallAudit(ReconValue):
    admission: CallAdmission
    completion: CallCompletion | None = None
    decision: StageDecision | None = None


def completed_audit(
    audit: CallAudit | None, completion: CallCompletion | None, now: datetime
) -> CallAudit | None:
    if audit is None:
        if completion is not None:
            raise ConflictError("call completion needs audited admission")
        return None
    if completion is not None:
        completion = CallCompletion.model_validate(completion.model_dump())
    if audit.completion is not None:
        if completion is not None and completion != audit.completion:
            raise ConflictError("call completion already recorded")
        return audit
    return audit.model_copy(
        update={"completion": completion or CallCompletion(reason="unreported", finished_at=now)}
    )


def decided_spend(value: ReconsolidationSpend, decision: StageDecision) -> ReconsolidationSpend:
    decision = StageDecision.model_validate(decision.model_dump())
    audit = value.call_audit
    if (
        value.state == "reserved"
        or audit is None
        or audit.completion is None
        or audit.completion.reason != "completed"
    ):
        raise ConflictError("stage decision needs a completed call")
    allowed = (
        {"prepared", "deferred", "invalid_response"}
        if audit.admission.stage == "proposal"
        else {"reviewed", "invalid_response"}
    )
    if decision.status not in allowed:
        raise ConflictError("stage decision does not match the admitted stage")
    if audit.decision is not None and audit.decision != decision:
        raise ConflictError("stage decision already recorded")
    return value.model_copy(update={"call_audit": audit.model_copy(update={"decision": decision})})


class ReconsolidationSpend(ReconValue):
    id: UUID
    tenant_id: str
    principal_id: str
    job_id: UUID
    lease_token: UUID
    day: date
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    maximum_usd: Decimal = Field(gt=0, le=SLICE_USD, decimal_places=10)
    charged_usd: Decimal = Field(ge=0, decimal_places=10)
    state: Literal["reserved", "settled", "unknown"] = "reserved"
    call_audit: CallAudit | None = None


class ReconsolidationAudit(ReconValue):
    job_id: UUID
    lease_token: UUID
    occurred_at: AwareDatetime
    inspected: int = Field(ge=0, le=128)
    excluded: int = Field(ge=0, le=128)
    selected_groups: int = Field(ge=0, le=4)
    not_selected: int = Field(ge=0, le=128)


def selected_sources(
    group: ReconsolidationGroup, source_ids: tuple[UUID, ...] | None
) -> tuple[SourceVersion, ...]:
    """A candidate can narrow its claimed group, never add another source."""
    if source_ids is None:
        return group.sources
    selected = tuple(s for s in group.sources if s.belief_id in source_ids)
    if len(selected) != len(source_ids) or not 2 <= len(set(source_ids)) <= 32:
        raise ConflictError("candidate sources are outside the claimed group")
    return selected


def group_digest(sources: tuple[SourceVersion, ...]) -> str:
    ordered = sorted((str(source.belief_id), source.content_revision) for source in sources)
    return hashlib.sha256(json.dumps([POLICY, ordered]).encode()).hexdigest()


def utc_now(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("reconsolidation requires aware time")
    return now.astimezone(UTC)


def check_lease(
    job: ReconsolidationJob | None, principal: Principal, token: UUID, now: datetime
) -> ReconsolidationJob:
    now = utc_now(now)
    if job is None or (job.tenant_id, job.principal_id) != (
        principal.tenant_id,
        principal.principal_id,
    ):
        raise NotFoundError("reconsolidation job not found")
    if job.lease_token != token or job.state != "running" or job.lease_expires_at <= now:
        raise ConflictError("reconsolidation lease lost")
    return job


def eligible(record: MemoryRecord, now: datetime) -> bool:
    return (
        record.status in LIVE_MEMORY_STATUSES
        and record.derivation is MemoryDerivation.DIRECT
        and record.sensitivity in PROVIDER_EGRESS_SENSITIVITIES
        and record.valid_from <= now
        and (record.expires_at is None or record.expires_at > now)
        and (record.valid_to is None or record.valid_to > now)
    )


class GroupingSource(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def statement(self) -> str: ...

    @property
    def scope(self) -> str: ...

    @property
    def portability(self) -> Portability: ...


def compatible(left: GroupingSource, right: GroupingSource) -> bool:
    return left.scope == right.scope and (
        left.scope != "user" or left.portability is right.portability is Portability.PORTABLE
    )


def neighbor_score(
    anchor: GroupingSource, candidate: GroupingSource, terms: list[str] | None = None
) -> tuple[int, int] | None:
    """Shared matching for volatile inventory and the provider-free input preview."""
    if not compatible(anchor, candidate):
        return None
    if terms is None:
        terms = lexical_query_terms(anchor.subject + " " + anchor.statement)[:64]
    exact = int(candidate.subject == anchor.subject)
    overlap = sum(
        lexical_text_matches(
            lexical_term_lexemes([term]), candidate.subject + " " + candidate.statement
        )
        for term in terms
    )
    return (exact, overlap) if exact or overlap else None
