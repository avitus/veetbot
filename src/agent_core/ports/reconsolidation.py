"""Durable bounded maintenance port for memory reconsolidation."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.derived_memory import SummaryAction, SummaryWriteReceipt
from agent_core.domain.memory import MemoryBrowseQuery, Sensitivity
from agent_core.domain.reconsolidation import (
    CallAdmission,
    CallCompletion,
    InventoryPage,
    ReconsolidationGroup,
    ReconsolidationJob,
    ReconsolidationSpend,
    SourceVersion,
    StageDecision,
)
from agent_core.domain.reconsolidation_inputs import ReconsolidationInput
from agent_core.domain.reconsolidation_merge import MergePlan
from agent_core.domain.reconsolidation_operations import (
    StoredConflict,
    StoredMerge,
    StoredOperation,
    StoredSummary,
)
from agent_core.domain.reconsolidation_summary import PreparedSummary, SummaryClause, SummaryMemory
from agent_core.domain.reconsolidation_views import (
    OperationCursor,
    OperationKind,
    OperationState,
    OperationView,
)


class ReconsolidationStore(Protocol):
    async def get_spend(
        self, principal: Principal, reservation_id: UUID
    ) -> ReconsolidationSpend: ...

    async def record_stage_decision(
        self,
        principal: Principal,
        token: UUID,
        reservation_id: UUID,
        decision: StageDecision,
        now: datetime,
    ) -> ReconsolidationSpend: ...

    async def browse_summaries(
        self, principal: Principal, query: MemoryBrowseQuery, now: datetime
    ) -> tuple[SummaryMemory, ...]:
        """Revalidate/filter before limit+1, using the original browse keyset ordering."""
        ...

    async def summary_rejection_targets(
        self, principal: Principal, operation_id: UUID
    ) -> tuple[UUID, ...]:
        """All owner summaries sharing rejection signatures, including purged projections."""
        ...

    async def change_summary(
        self, principal: Principal, operation_id: UUID, action: SummaryAction, now: datetime
    ) -> StoredSummary:
        """Caller owns write visibility, copy erasure and the atomic receipt transaction."""
        ...

    async def summary_write_receipt(
        self, principal: Principal, key: str
    ) -> SummaryWriteReceipt | None: ...

    async def record_summary_write(
        self, principal: Principal, key: str, receipt: SummaryWriteReceipt
    ) -> None: ...

    async def operation_page(
        self,
        principal: Principal,
        *,
        kind: OperationKind | None,
        state: OperationState | None,
        before: OperationCursor | None,
        limit: int,
    ) -> tuple[StoredOperation, ...]:
        """Internal owner-scoped keyset page; at most 100 opaque operations."""
        ...

    async def get_operation(
        self, principal: Principal, operation_id: UUID, now: datetime, *, ceiling: Sensitivity
    ) -> OperationView | None:
        """Revalidate complete support and return an allow-listed owner view."""
        ...

    async def merges_at(
        self,
        principal: Principal,
        member_ids: tuple[UUID, ...],
        *,
        as_of: datetime,
        known_at: datetime | None,
    ) -> tuple[StoredMerge, ...]:
        """Read-only historical membership; exact source revisions and current privacy.

        At most 1000 candidates. No knowledge cutoff means current source knowledge.
        """
        ...

    async def active_merges(
        self, principal: Principal, member_ids: tuple[UUID, ...], now: datetime
    ) -> tuple[StoredMerge, ...]:
        """Internal live membership for at most 1000 candidates; revalidate all support."""
        ...

    async def plan_summary(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        clauses: tuple[SummaryClause, ...],
        now: datetime,
        *,
        source_ids: tuple[UUID, ...] | None = None,
        kind: Literal["summary", "hypothesis"] = "summary",
    ) -> PreparedSummary | None: ...

    async def active_summaries(
        self,
        principal: Principal,
        member_ids: tuple[UUID, ...],
        now: datetime,
        *,
        ceiling: Sensitivity,
        current_scope: str,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
        min_store_position: int = 0,
    ) -> tuple[SummaryMemory, ...]:
        """At most 1000 summary candidates linked to matched atoms or newer than a delta.

        Revalidate every dependency and visibility. Historical queries reconstruct
        exact original clauses without retaining text or changing current state.
        """
        ...

    async def update_summary_usage(
        self,
        principal: Principal,
        operation_id: UUID,
        delta: float,
        now: datetime,
        *,
        cited: bool,
    ) -> bool:
        """Revalidate support, then move derived usage only; never republish or reinforce atoms."""
        ...

    async def summary_operation(
        self, principal: Principal, operation_id: UUID
    ) -> StoredSummary | None:
        """Internal content-free state; use get_summary to revalidate current support."""
        ...

    async def commit_conflict(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        source_ids: tuple[UUID, ...],
        now: datetime,
        *,
        complete_group: bool = True,
        model_identity: str = "deterministic-extractive@1",
    ) -> StoredConflict | None: ...

    async def commit_summary(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        expected: PreparedSummary,
        now: datetime,
        *,
        complete_group: bool = True,
        model_identity: str = "deterministic-extractive@1",
    ) -> StoredSummary:
        """Internal extractive commit; exact versions and all current evidence are rechecked."""
        ...

    async def get_summary(
        self,
        principal: Principal,
        operation_id: UUID,
        now: datetime,
        *,
        ceiling: Sensitivity,
        current_scope: str,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> SummaryMemory | None:
        """Validate all support; purge invalid current projections.

        With either historical clock, reconstruct permitted original clauses without
        mutations. Current privacy applies to every revision; no subject override.
        """
        ...

    async def commit_merge(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        expected: MergePlan,
        now: datetime,
        *,
        complete_group: bool = True,
    ) -> StoredMerge:
        """Recheck a preview and atomically persist a deterministic merge."""
        ...

    async def get_merge(
        self,
        principal: Principal,
        operation_id: UUID,
        now: datetime,
    ) -> StoredMerge | None:
        """Internal identity-only metadata; revalidate committed support before reading.

        This is not a public OperationView and never returns original prose.
        """
        ...

    async def merge_members(
        self,
        principal: Principal,
        operation_id: UUID,
        now: datetime,
    ) -> tuple[UUID, ...]: ...

    async def undo_merge(
        self,
        principal: Principal,
        operation_id: UUID,
        expected_revision: int,
        idempotency_key: str,
        now: datetime,
    ) -> StoredMerge:
        """Persist undo and a receipt; replay returns only opaque metadata."""
        ...

    async def original_input(
        self, principal: Principal, token: UUID, group_id: UUID, now: datetime
    ) -> ReconsolidationInput | None:
        """Read a claimed group's complete admitted originals without provider egress."""
        ...

    async def plan_merge(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        now: datetime,
        *,
        source_ids: tuple[UUID, ...] | None = None,
    ) -> MergePlan | None:
        """Inspect a claimed group using current evidence; never authorize a commit."""
        ...

    async def claim_due(
        self, principal: Principal, now: datetime, lease_owner: str
    ) -> ReconsolidationJob | None: ...

    async def renew(
        self, principal: Principal, token: UUID, now: datetime
    ) -> ReconsolidationJob: ...

    async def inventory(
        self, principal: Principal, token: UUID, now: datetime
    ) -> InventoryPage: ...

    async def neighbors(
        self, principal: Principal, source: SourceVersion, now: datetime
    ) -> tuple[SourceVersion, ...]: ...

    async def checkpoint(
        self,
        principal: Principal,
        token: UUID,
        page: InventoryPage,
        groups: tuple[tuple[SourceVersion, ...], ...],
        now: datetime,
    ) -> tuple[ReconsolidationGroup, ...]: ...

    async def release(
        self, principal: Principal, token: UUID, now: datetime
    ) -> ReconsolidationJob: ...

    async def claim_group(
        self, principal: Principal, token: UUID, now: datetime
    ) -> ReconsolidationGroup | None: ...

    async def finish_group(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        outcome: Literal["no_change", "retry", "committed"],
        now: datetime,
    ) -> ReconsolidationGroup: ...

    async def reserve(
        self,
        principal: Principal,
        token: UUID,
        request_digest: str,
        maximum_usd: Decimal,
        now: datetime,
        *,
        admission: CallAdmission | None = None,
    ) -> ReconsolidationSpend: ...

    async def settle(
        self,
        principal: Principal,
        token: UUID,
        reservation_id: UUID,
        actual_usd: Decimal | None,
        now: datetime,
        *,
        completion: CallCompletion | None = None,
    ) -> ReconsolidationSpend: ...
