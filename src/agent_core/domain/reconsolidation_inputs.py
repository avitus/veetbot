"""Transient authenticated originals and explicit local provider-egress decisions."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.memory import Portability, Sensitivity
from agent_core.domain.messages import ModelRequest
from agent_core.domain.reconsolidation import ReconsolidationGroup, ReconValue
from agent_core.domain.reconsolidation_merge import AttributionRole, Digest, MergeSource
from agent_core.domain.reconsolidation_provider import (
    CandidateReview,
    ProposalContext,
    ProposalEnvelope,
)


class OriginalExcerpt(ReconValue):
    """A complete text part of an authenticated owner event, never a substring."""

    id: UUID
    event_id: UUID
    session_id: UUID
    event_sequence: int = Field(gt=0)
    part_index: int = Field(ge=0, lt=32)
    part_count: int = Field(ge=1, le=32)
    occurred_at: AwareDatetime
    text: str = Field(min_length=1, max_length=2048)


class ReconsolidationInput(ReconValue):
    """Repository preview under owner/People locks; not a send or commit permit."""

    group: ReconsolidationGroup
    sources: tuple[MergeSource, ...] = Field(min_length=2, max_length=32)
    excerpts: tuple[OriginalExcerpt, ...] = Field(min_length=1, max_length=256)


class EgressSubject(ReconValue):
    kind: Literal["memory", "excerpt", "proposal"]
    id: UUID
    text: str
    sensitivity_floor: Sensitivity | None
    scope: str
    portability: Portability
    attribution: tuple[tuple[AttributionRole, str], ...]


class ReconsolidationEgressDecision(ReconValue):
    """A current local policy answer; unknown classification/residency denies egress."""

    tenant_id: str
    principal_id: str
    provider: str
    model: str
    policy_version: str = Field(min_length=1, max_length=128)
    assessed_at: AwareDatetime
    permitted: bool
    sensitivity: Sensitivity | None
    residency: Literal["allowed", "denied", "unknown"]


InputDeferralReason = Literal[
    "source_unavailable",
    "source_ineligible",
    "scope_mismatch",
    "egress_unavailable",
    "egress_denied",
    "input_budget",
    "model_unavailable",
    "pricing_unavailable",
    "cost_budget",
]


class InputDeferral(ReconValue):
    group_id: UUID
    reason: InputDeferralReason


class PreparedProposalRequest(ReconValue):
    context: ProposalContext
    job_id: UUID
    lease_token: UUID
    serialized_request: str
    request_digest: Digest
    egress_policy_version: str
    estimated_input_tokens: int = Field(gt=0, le=16384)
    maximum_cost_usd: Decimal = Field(gt=0, le=Decimal("0.25"))
    prepared_at: AwareDatetime

    @property
    def request(self) -> ModelRequest:
        # Return an independent copy: caller mutation cannot alter the priced bytes.
        return ModelRequest.model_validate_json(self.serialized_request)


class ProposalPreparation(ReconValue):
    prepared: PreparedProposalRequest | None
    deferred: tuple[InputDeferral, ...]


class PreparedVerificationRequest(PreparedProposalRequest):
    proposal: ProposalEnvelope
    local_rejections: tuple[CandidateReview, ...]


class VerificationPreparation(ReconValue):
    prepared: PreparedVerificationRequest | None
    rejected: tuple[CandidateReview, ...]
    deferred: tuple[InputDeferral, ...]
