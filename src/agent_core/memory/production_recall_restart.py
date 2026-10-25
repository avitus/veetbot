"""A separately assessed ADR-0223 restart, bounded by the original trial clock."""

import hashlib
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.agents import Principal
from agent_core.domain.general_recall_release import Digest, Identifier, ReleaseValue
from agent_core.memory.general_recall_evidence import _require
from agent_core.memory.production_recall_trial import (
    ProductionRecallCheckIn,
    ProductionRecallTrial,
    validate_check_ins,
)
from agent_core.ports.determinism import Clock


class ProductionRecallRestart(ReleaseValue):
    version: Literal["recall-owner-production-restart@1"]
    trial_id: Identifier
    tenant_id: Identifier
    principal_id: Identifier
    owner_kind: Literal["human"]
    decision: Literal["restart"]
    authorized_at: AwareDatetime
    restarted_at: AwareDatetime
    instruction: str = Field(min_length=20, max_length=10000)
    owner_response_ref: Identifier
    owner_statement_sha256: Digest
    prior_trial_sha256: Digest
    prior_evidence_sha256: Digest
    previous_expiry_at: AwareDatetime
    assessment_sha256: Digest
    withdrawal_receipt_sha256: Digest
    target_inputs_sha256: Digest
    rollback_build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    check_ins: tuple[ProductionRecallCheckIn, ...] = Field(max_length=28)


def validate_restart_window(
    trial: ProductionRecallTrial,
    restart: ProductionRecallRestart,
    activated_at: datetime,
    expires_at: datetime,
    target_inputs_sha256: str,
    rollback_build_ref: str,
    principal: Principal,
    clock: Clock,
) -> None:
    """Preserve the old phase; require a separate decision and a new review window.

    Hashes identify privately retained evidence, not authenticated authorship or
    independently verified compatibility. Those remain operator release gates.
    """
    owner = (principal.tenant_id, principal.principal_id)
    end = activated_at + timedelta(days=7)
    now = clock.now()
    _require(
        (trial.tenant_id, trial.principal_id) == owner
        and (restart.tenant_id, restart.principal_id) == owner
        and restart.trial_id == trial.trial_id
        and trial.started_at == activated_at
        and trial.authorized_at <= activated_at
        and len(trial.instruction.strip()) >= 20
        and restart.prior_trial_sha256
        == hashlib.sha256(trial.model_dump_json().encode()).hexdigest()
        and restart.target_inputs_sha256 == target_inputs_sha256
        and restart.rollback_build_ref == rollback_build_ref
        and len(restart.instruction.strip()) >= 20
        and restart.owner_response_ref.strip() != ""
        and restart.previous_expiry_at <= restart.authorized_at <= restart.restarted_at <= now
        and now < expires_at <= end,
        "restart requires a separate owner decision, preserved history and assessed current inputs",
    )
    seen: set[UUID] = set()
    responses: set[str] = set()
    previous = validate_check_ins(
        trial.check_ins, trial.trial_id, owner, activated_at, now, seen, responses
    )
    _require(
        restart.previous_expiry_at == min(previous + timedelta(hours=24), end)
        and restart.owner_response_ref not in responses,
        "restart cannot rewrite the previous expiry or reuse an earlier response",
    )
    responses.add(restart.owner_response_ref)
    previous = validate_check_ins(
        restart.check_ins, trial.trial_id, owner, restart.restarted_at, now, seen, responses
    )
    _require(
        expires_at == min(previous + timedelta(hours=24), end),
        "restart requires timely real-use review within 24 hours and the original seven-day end",
    )
