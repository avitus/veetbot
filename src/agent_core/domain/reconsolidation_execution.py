"""Ephemeral execution results; none of these values authorizes a memory write."""

from typing import Literal
from uuid import UUID

from agent_core.domain.reconsolidation import ReconsolidationSpend, ReconValue
from agent_core.domain.reconsolidation_inputs import PreparedVerificationRequest
from agent_core.domain.reconsolidation_provider import ProposalReview

type ExecutionReason = Literal[
    "reviewed",
    "deferred",
    "unavailable",
    "admission_withdrawn",
    "lease_lost",
    "timeout",
    "invalid_response",
    "settlement_failed",
]


class ReconsolidationCall(ReconValue):
    stage: Literal["proposal", "verification"]
    spend: ReconsolidationSpend
    reason: ExecutionReason
    input_tokens: int | None = None
    output_tokens: int | None = None


class ReconsolidationExecution(ReconValue):
    reason: ExecutionReason
    calls: tuple[ReconsolidationCall, ...] = ()
    # Transient, untrusted content. Do not persist this result as an audit record.
    prepared: PreparedVerificationRequest | None = None
    review: ProposalReview | None = None


class AppliedGroup(ReconValue):
    group_id: UUID
    outcome: Literal["committed", "no_change", "retry", "deferred"]
    operation_ids: tuple[UUID, ...] = ()
