"""Content-free cursors, leases and conservative embedding spend (M33)."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.semantic_memory import EmbeddingSpace

QUANTUM = Decimal("0.00000001")


class SemanticValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EmbeddingConfig(SemanticValue):
    space: EmbeddingSpace
    usd_per_million_tokens: Decimal = Field(ge=0, allow_inf_nan=False)
    pricing_revision: str = Field(min_length=1, max_length=128)
    egress_revision: str = Field(min_length=1, max_length=128)
    residency_provider: str = Field(min_length=1, max_length=128)
    request_usd: Decimal = Field(gt=0, le=Decimal("0.25"), allow_inf_nan=False)
    daily_usd: Decimal = Field(gt=0, le=Decimal("2"), allow_inf_nan=False)

    @property
    def key(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def price(self, tokens: int) -> Decimal:
        if not 0 <= tokens <= 65536:
            raise ValueError("embedding token ceiling exceeded")
        return (Decimal(tokens) * self.usd_per_million_tokens / 1_000_000).quantize(
            QUANTUM, rounding=ROUND_CEILING
        )


class SemanticJob(SemanticValue):
    tenant_id: str
    principal_id: str
    config_key: str
    generation: int = Field(ge=1)
    full_bound: int = Field(ge=0)
    change_bound: int = Field(ge=0)
    full_cursor: int = Field(default=0, ge=0)
    change_cursor: int = Field(default=0, ge=0)
    next_full: int = Field(default=0, ge=0)
    next_change: int = Field(default=0, ge=0)
    lease_token: UUID
    lease_expires_at: AwareDatetime
    deadline: AwareDatetime
    day: date
    request_id: UUID | None = None
    attempts: int = Field(default=0, ge=0, le=3)
    failed_pages: int = Field(default=0, ge=0)
    state: Literal["running", "ready", "complete"] = "running"


class SemanticSpend(SemanticValue):
    id: UUID
    tenant_id: str
    principal_id: str
    config_key: str
    lease_token: UUID
    day: date
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    maximum_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    actual_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    state: Literal["reserved", "known", "unknown"] = "reserved"

    kind: Literal["index", "query"] = "index"
    deadline: AwareDatetime | None = None

    @model_validator(mode="after")
    def query_deadline(self) -> Self:
        if self.kind == "query" and self.deadline is None:
            raise ValueError("query reservation requires a deadline")
        return self

    @property
    def charge(self) -> Decimal:
        return self.actual_usd if self.actual_usd is not None else self.maximum_usd


def require_job(job: SemanticJob | None, token: UUID, now: AwareDatetime) -> SemanticJob:
    from agent_core.domain.errors import ConflictError

    if job is None or job.lease_token != token or job.state != "running" or now >= job.deadline:
        raise ConflictError("semantic index lease unavailable")
    return job


def check_cursors(job: SemanticJob, full: int, changed: int) -> None:
    from agent_core.domain.errors import ConflictError

    if not (
        job.full_cursor <= full <= min(job.full_bound, job.full_cursor + 16)
        and job.change_cursor <= changed <= min(job.change_bound, job.change_cursor + 16)
    ):
        raise ConflictError("semantic inventory checkpoint exceeds bounded page")


def finish_job(job: SemanticJob, full: int, changed: int, success: bool) -> SemanticJob:
    from agent_core.domain.errors import ConflictError

    check_cursors(job, full, changed)
    if job.request_id is not None and (full, changed) != (job.next_full, job.next_change):
        raise ConflictError("semantic request checkpoint changed")
    advance = success or job.attempts >= 3
    if not advance:
        return job.model_copy(update={"state": "ready"})
    return job.model_copy(
        update={
            "full_cursor": full,
            "change_cursor": changed,
            "attempts": 0,
            "failed_pages": job.failed_pages + int(not success),
            "state": "complete"
            if (full, changed) == (job.full_bound, job.change_bound)
            else "ready",
        }
    )


def claim_job(
    old: SemanticJob | None,
    *,
    tenant: str,
    owner: str,
    config_key: str,
    full_bound: int,
    change_bound: int,
    token: UUID,
    now: AwareDatetime,
) -> SemanticJob | None:
    from datetime import UTC, timedelta

    if old is not None and old.state == "running" and now < old.lease_expires_at:
        return None
    if old is not None and old.attempts >= 3:
        old = finish_job(old, old.next_full, old.next_change, False)
    day = now.astimezone(UTC).date()
    reset = old is None or old.config_key != config_key or old.state == "complete"
    if (
        old is not None
        and old.config_key == config_key
        and old.state == "complete"
        and (old.day == day and old.full_bound == full_bound and old.change_bound == change_bound)
    ):
        return None
    if reset:
        old = SemanticJob(
            tenant_id=tenant,
            principal_id=owner,
            config_key=config_key,
            generation=old.generation + 1 if old else 1,
            full_bound=full_bound,
            change_bound=change_bound,
            change_cursor=old.change_bound
            if old is not None and old.config_key == config_key
            else 0,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=90),
            deadline=now + timedelta(seconds=60),
            day=day,
        )
    assert old is not None
    return old.model_copy(
        update={
            "lease_token": token,
            "lease_expires_at": now + timedelta(seconds=90),
            "deadline": now + timedelta(seconds=60),
            "day": day,
            "state": "running",
            "request_id": None,
        }
    )


def settled_spend(spend: SemanticSpend, actual: Decimal | None) -> SemanticSpend:
    from agent_core.domain.errors import ConflictError

    if actual is not None and (not actual.is_finite() or actual < 0 or actual > spend.maximum_usd):
        raise ConflictError("semantic usage exceeds reservation")
    if actual is not None:
        actual = actual.quantize(QUANTUM, rounding=ROUND_CEILING)
    if spend.state != "reserved":
        if spend.actual_usd == actual:
            return spend
        raise ConflictError("semantic spend already settled")
    return spend.model_copy(
        update={"actual_usd": actual, "state": "unknown" if actual is None else "known"}
    )


def require_query(spend: SemanticSpend | None, now: datetime) -> SemanticSpend:
    from agent_core.domain.errors import ConflictError

    if spend is None or spend.kind != "query" or spend.deadline is None or now >= spend.deadline:
        raise ConflictError("semantic query reservation unavailable")
    return spend


def query_reservation(
    tenant: str,
    owner: str,
    config: EmbeddingConfig,
    digest: str,
    maximum: Decimal,
    spent: Decimal,
    request_id: UUID,
    now: datetime,
) -> SemanticSpend | None:
    if not maximum.is_finite():
        return None
    maximum = maximum.quantize(QUANTUM, rounding=ROUND_CEILING)
    if not 0 < maximum <= config.request_usd or spent + maximum > config.daily_usd:
        return None
    return SemanticSpend(
        id=request_id,
        tenant_id=tenant,
        principal_id=owner,
        config_key=config.key,
        lease_token=request_id,
        day=now.astimezone(UTC).date(),
        request_digest=digest,
        maximum_usd=maximum,
        kind="query",
        deadline=now + timedelta(seconds=15),
    )
