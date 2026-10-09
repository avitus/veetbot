"""Durable operation metadata contains identities and hashes, never original prose."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from agent_core.domain.agents import Principal
from agent_core.domain.reconsolidation import POLICY, ReconValue
from agent_core.domain.reconsolidation_merge import (
    Digest,
    MergeDependency,
    MergeOperation,
    MergePlan,
)
from agent_core.domain.reconsolidation_summary import SummaryPlan


class StoredMerge(MergeOperation):
    kind: Literal["merge"] = "merge"
    group_id: UUID
    job_id: UUID
    input_digest: Digest
    policy: Literal["reconsolidation@1"] = POLICY
    model_identity: Literal["deterministic-equivalence@1"] = "deterministic-equivalence@1"
    evidence_identity: Digest
    created_at: AwareDatetime
    store_position: int = Field(gt=0)
    reason: Literal["equivalent", "source_changed", "owner_undo"] = "equivalent"


class StoredSummary(ReconValue):
    id: UUID
    kind: Literal["summary", "hypothesis"] = "summary"
    plan: SummaryPlan
    state: Literal["committed", "invalidated"] = "committed"
    revision: int = Field(default=1, ge=1)
    group_id: UUID
    job_id: UUID
    input_digest: Digest
    policy: Literal["reconsolidation@1"] = POLICY
    model_identity: str = "deterministic-extractive@1"
    evidence_identity: Digest
    created_at: AwareDatetime
    committed_at: AwareDatetime
    invalidated_at: AwareDatetime | None = None
    store_position: int = Field(gt=0)
    reason: Literal[
        "summarized",
        "inferred",
        "source_changed",
        "owner_reviewed",
        "owner_not_here",
        "owner_rejection",
        "owner_delete",
    ] = "summarized"
    rejection_signatures: tuple[Digest, ...] = ()
    owner_revision: int = Field(default=1, ge=1)
    reviewed: bool = False
    local_only: bool = False
    owner_removed: Literal["untrue", "delete"] | None = None
    updated_at: AwareDatetime | None = None


class ConflictPlan(ReconValue):
    tenant_id: str
    principal_id: str
    member_ids: tuple[UUID, ...] = Field(min_length=2, max_length=32)
    dependencies: tuple[MergeDependency, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def complete_lineage(self) -> "ConflictPlan":
        if self.member_ids != tuple(sorted(set(self.member_ids))) or self.member_ids != tuple(
            dep.source.belief_id for dep in self.dependencies
        ):
            raise ValueError("conflict requires complete ordered original dependencies")
        return self


class StoredConflict(ReconValue):
    id: UUID
    kind: Literal["conflict"] = "conflict"
    plan: ConflictPlan
    state: Literal["committed", "invalidated"] = "committed"
    revision: int = Field(default=1, ge=1)
    group_id: UUID
    job_id: UUID
    input_digest: Digest
    policy: Literal["reconsolidation@1"] = POLICY
    model_identity: str
    evidence_identity: Digest
    created_at: AwareDatetime
    committed_at: AwareDatetime
    invalidated_at: AwareDatetime | None = None
    store_position: int = Field(gt=0)
    reason: Literal["conflict_flagged", "source_changed"] = "conflict_flagged"


StoredOperation = StoredMerge | StoredSummary | StoredConflict
STORED_OPERATION: TypeAdapter[StoredOperation] = TypeAdapter(
    Annotated[StoredOperation, Field(discriminator="kind")]
)


def evidence_identity(plan: "MergePlan | SummaryPlan | ConflictPlan") -> str:
    return hashlib.sha256(
        json.dumps([d.model_dump(mode="json") for d in plan.dependencies], sort_keys=True).encode()
    ).hexdigest()


def undo_key(principal: "Principal", key: str) -> str:
    if not key.strip() or len(key) > 128:
        raise ValueError("undo requires a bounded idempotency key")
    return hashlib.sha256(
        json.dumps(
            [principal.tenant_id, principal.principal_id, key],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
