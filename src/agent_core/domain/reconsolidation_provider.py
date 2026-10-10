"""Closed, transient provider proposals; these values never authorize a write."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from agent_core.domain.reconsolidation import ReconValue
from agent_core.domain.reconsolidation_merge import Digest


class ProposalSourceRef(ReconValue):
    belief_id: UUID
    content_revision: int = Field(ge=1)


class OfferedSource(ReconValue):
    """Locally supplied identity binding, not provider-authored evidence."""

    source: ProposalSourceRef
    excerpt_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def distinct_excerpts(self) -> "OfferedSource":
        if len(set(self.excerpt_ids)) != len(self.excerpt_ids):
            raise ValueError("excerpt identities must be distinct")
        return self


class OfferedGroup(ReconValue):
    id: UUID
    sources: tuple[OfferedSource, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def distinct_sources(self) -> "OfferedGroup":
        if len({s.source.belief_id for s in self.sources}) != len(self.sources):
            raise ValueError("source identities must be distinct")
        return self


class ProposalContext(ReconValue):
    """Owner-bound identity snapshot; admission and egress precede its creation."""

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    batch_id: UUID
    groups: tuple[OfferedGroup, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unambiguous_bindings(self) -> "ProposalContext":
        if len({group.id for group in self.groups}) != len(self.groups):
            raise ValueError("group identities must be distinct")
        seen: dict[UUID, OfferedSource] = {}
        for group in self.groups:
            for offered in group.sources:
                key = offered.source.belief_id
                if key in seen and seen[key] != offered:
                    raise ValueError("shared source bindings must agree")
                seen[key] = offered
        return self


class ClauseSupport(ReconValue):
    belief_id: UUID
    excerpt_id: UUID


class ProposalClause(ReconValue):
    text: str = Field(min_length=1, max_length=4000)
    support: tuple[ClauseSupport, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def nonempty_distinct_support(self) -> "ProposalClause":
        if not self.text.strip() or len(set(self.support)) != len(self.support):
            raise ValueError("clause needs text and distinct support")
        return self


class ProposedOperation(ReconValue):
    id: UUID
    group_id: UUID
    kind: Literal[
        "merge_equivalent", "summarize_related", "infer_connection", "flag_conflict", "no_change"
    ]
    inputs: tuple[ProposalSourceRef, ...] = Field(min_length=2, max_length=32)
    clauses: tuple[ProposalClause, ...] = Field(max_length=4)

    @model_validator(mode="after")
    def closed_kind_shape(self) -> "ProposedOperation":
        if len({source.belief_id for source in self.inputs}) != len(self.inputs):
            raise ValueError("operation inputs must be distinct")
        counts = {
            "merge_equivalent": (1,),
            "summarize_related": (1, 2, 3, 4),
            "infer_connection": (1,),
            "flag_conflict": (0,),
            "no_change": (0,),
        }
        if len(self.clauses) not in counts[self.kind]:
            raise ValueError("clause count does not match operation kind")
        return self


class ProposalEnvelope(ReconValue):
    schema_version: Literal["reconsolidation-proposal@1"]
    batch_id: UUID
    group_ids: tuple[UUID, ...] = Field(min_length=1, max_length=4)
    operations: tuple[ProposedOperation, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def complete_groups(self) -> "ProposalEnvelope":
        if len(set(self.group_ids)) != len(self.group_ids):
            raise ValueError("group identities must be distinct")
        if len({op.id for op in self.operations}) != len(self.operations):
            raise ValueError("operation identities must be distinct")
        if {op.group_id for op in self.operations} != set(self.group_ids):
            raise ValueError("every declared group needs its outcome")
        for group_id in self.group_ids:
            group_ops = [op for op in self.operations if op.group_id == group_id]
            if len(group_ops) > 1 and any(op.kind == "no_change" for op in group_ops):
                raise ValueError("no_change cannot accompany another group operation")
        return self


class ClauseVerdict(ReconValue):
    operation_id: UUID
    clause_index: int = Field(ge=0, le=3)
    status: Literal["supported", "unsupported", "uncertain"]
    reason: Literal[
        "entailed", "contradicted", "insufficient_evidence", "ambiguous", "policy_excluded"
    ]

    @model_validator(mode="after")
    def consistent_reason(self) -> "ClauseVerdict":
        permitted = {
            "supported": {"entailed"},
            "unsupported": {"contradicted", "policy_excluded"},
            "uncertain": {"insufficient_evidence", "ambiguous"},
        }
        if self.reason not in permitted[self.status]:
            raise ValueError("verification status and reason disagree")
        return self


class VerificationEnvelope(ReconValue):
    schema_version: Literal["reconsolidation-verification@1"]
    batch_id: UUID
    proposal_digest: Digest
    verdicts: tuple[ClauseVerdict, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def distinct_verdicts(self) -> "VerificationEnvelope":
        if len({(v.operation_id, v.clause_index) for v in self.verdicts}) != len(self.verdicts):
            raise ValueError("each clause must be verified exactly once")
        return self


CandidateReason = Literal[
    "verified",
    "unknown_source",
    "stale_source",
    "unknown_excerpt",
    "invalid_support",
    "unsafe_output",
    "unsupported_clause",
    "uncertain_clause",
    "input_unavailable",
]


class CandidateReview(ReconValue):
    operation: ProposedOperation
    reason: CandidateReason

    @property
    def requires_local_validation(self) -> bool:
        return self.reason == "verified"


class ProposalReview(ReconValue):
    tenant_id: str
    principal_id: str
    batch_id: UUID
    proposal_digest: Digest
    candidates: tuple[CandidateReview, ...]
