"""Owner review is separate from autonomous reconsolidation admission."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from agent_core.domain.reconsolidation import ReconValue


class OwnerDreamingReview(ReconValue):
    decision: Literal["pending", "approved", "rejected"] = "pending"
    decided_at: AwareDatetime | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    idempotency_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def decision_receipt(self) -> "OwnerDreamingReview":
        values = (self.decided_at, self.expected_revision, self.idempotency_digest)
        if self.decision == "pending" and any(v is not None for v in values):
            raise ValueError("a pending proposal has no decision receipt")
        if self.decision != "pending" and any(v is None for v in values):
            raise ValueError("a decision needs a complete receipt")
        return self


def review_visible(
    review: OwnerDreamingReview | None,
    now: datetime,
    *,
    as_of: datetime | None = None,
    known_at: datetime | None = None,
) -> bool:
    if review is None:
        return True
    cutoff = min(now, as_of or now, known_at or now)
    return (
        review.decision == "approved"
        and review.decided_at is not None
        and review.decided_at <= cutoff
    )


class DreamingSchedule(ReconValue):
    paused: bool = False
    next_run_at: AwareDatetime | None = None
    revision: int = Field(default=1, ge=1)


class DreamingRun(ReconValue):
    id: UUID
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    outcome: Literal["running", "proposed", "no_change", "blocked", "failed", "interrupted"] = (
        "running"
    )
    reason: Literal[
        "running",
        "reviewed",
        "deferred",
        "unavailable",
        "admission_withdrawn",
        "lease_lost",
        "timeout",
        "invalid_response",
        "settlement_failed",
        "no_work_available",
        "execution_failed",
        "unreported",
    ] = "running"
    proposals: int = Field(default=0, ge=0, le=8)
    provider_calls: int = Field(default=0, ge=0, le=2)
    charged_usd: Decimal | None = Field(default=None, ge=0, le=1)


class DreamingStatus(ReconValue):
    schedule: DreamingSchedule
    runs: tuple[DreamingRun, ...]


class DreamingDecisionRequest(ReconValue):
    expected_revision: int = Field(strict=True, ge=1)
    decision: Literal["approved", "rejected"]


class DreamingPauseRequest(ReconValue):
    expected_revision: int = Field(strict=True, ge=1)
    paused: bool = Field(strict=True)
