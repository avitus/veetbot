"""ADR-0223 trial authorization and real-use check-ins, not synthetic findings."""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.agents import Principal
from agent_core.domain.general_recall_release import Digest, Identifier, ReleaseValue
from agent_core.memory.general_recall_evidence import _require
from agent_core.ports.determinism import Clock


class ProductionRecallCheckIn(ReleaseValue):
    trial_id: Identifier
    tenant_id: Identifier
    principal_id: Identifier
    reviewer_kind: Literal["human"]
    reviewed_at: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    decision: Literal["continue", "stop", "changes_required"]
    owner_response_ref: Identifier
    owner_statement_sha256: Digest
    eligible_run_count: int = Field(strict=True, ge=0, le=100000)
    reviewed_runs: tuple[UUID, ...] = Field(max_length=5)
    selection_receipt_sha256: Digest
    provenance_verification_sha256: Digest
    serious_incident: bool = Field(strict=True)


class ProductionRecallTrial(ReleaseValue):
    version: Literal["recall-owner-production-trial@1"]
    trial_id: Identifier
    tenant_id: Identifier
    principal_id: Identifier
    owner_kind: Literal["human"]
    authorized_at: AwareDatetime
    instruction: str = Field(min_length=20, max_length=10000)
    authorization_sha256: Digest
    observations_sha256: Digest
    review_items_sha256: Digest
    started_at: AwareDatetime
    check_ins: tuple[ProductionRecallCheckIn, ...] = Field(max_length=28)


def validate_trial_window(
    trial: ProductionRecallTrial,
    activated_at: datetime,
    expires_at: datetime,
    principal: Principal,
    clock: Clock,
) -> None:
    """A timely genuine check-in permits one more day, never a new seven-day clock.

    As with other operator evidence, receipts bind bytes, not human authorship.
    Actual owner statements and authorized selection require operator verification.
    """
    owner = (principal.tenant_id, principal.principal_id)
    _require(
        (trial.tenant_id, trial.principal_id) == owner
        and trial.started_at == activated_at
        and trial.authorized_at <= activated_at
        and len(trial.instruction.strip()) >= 20
        and activated_at <= clock.now() < expires_at
        and expires_at <= activated_at + timedelta(days=7),
        "production trial owner, authorization, start or lifetime mismatch",
    )
    previous = validate_check_ins(
        trial.check_ins, trial.trial_id, owner, activated_at, clock.now(), set(), set()
    )
    _require(
        previous <= clock.now() < expires_at
        and expires_at == min(previous + timedelta(hours=24), activated_at + timedelta(days=7)),
        "production trial requires a new owner check-in within 24 hours",
    )


def validate_check_ins(
    reviews: tuple[ProductionRecallCheckIn, ...],
    trial_id: str,
    owner: tuple[str, str],
    started_at: datetime,
    now: datetime,
    seen: set[UUID],
    responses: set[str],
) -> datetime:
    """Validate a continuous review window and accumulate used receipts and runs."""
    previous = started_at
    through = started_at
    for review in reviews:
        _require(
            review.trial_id == trial_id
            and (review.tenant_id, review.principal_id) == owner
            and review.window_start == through
            and through < review.window_end <= review.reviewed_at
            and previous <= review.window_end
            and previous < review.reviewed_at < previous + timedelta(hours=24)
            and review.reviewed_at <= now
            and review.decision == "continue"
            and not review.serious_incident
            and review.owner_response_ref.strip() != ""
            and review.owner_response_ref not in responses
            and len(review.reviewed_runs) == min(review.eligible_run_count, 5)
            and len(set(review.reviewed_runs)) == len(review.reviewed_runs)
            and seen.isdisjoint(review.reviewed_runs),
            "production check-in is late, incomplete, mismatched or refuses continuation",
        )
        previous = review.reviewed_at
        through = review.window_end
        seen.update(review.reviewed_runs)
        responses.add(review.owner_response_ref)
    return previous
