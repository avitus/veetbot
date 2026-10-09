"""Reconsolidation state shares original memory's owner transaction guards."""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from agent_core.adapters.memory.reconsolidation_evidence import ReconsolidationEvidence
from agent_core.domain.agents import Principal
from agent_core.domain.derived_memory import (
    SummaryAction,
    SummaryWriteReceipt,
    change_summary,
    controlled_summary,
    summary_blocks,
    summary_matches,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    MemoryBrowseQuery,
    Portability,
    Sensitivity,
    lexical_query_terms,
)
from agent_core.domain.reconsolidation import (
    DAY_USD,
    SLICE_USD,
    CallAdmission,
    CallAudit,
    CallCompletion,
    InventoryPage,
    ReconsolidationAudit,
    ReconsolidationGroup,
    ReconsolidationJob,
    ReconsolidationSpend,
    SourceVersion,
    StageDecision,
    check_lease,
    compatible,
    completed_audit,
    decided_spend,
    eligible,
    group_digest,
    neighbor_score,
    selected_sources,
    utc_now,
)
from agent_core.domain.reconsolidation_inputs import ReconsolidationInput
from agent_core.domain.reconsolidation_merge import MergePlan, undo_merge
from agent_core.domain.reconsolidation_operations import (
    ConflictPlan,
    StoredConflict,
    StoredMerge,
    StoredOperation,
    StoredSummary,
    evidence_identity,
    undo_key,
)
from agent_core.domain.reconsolidation_summary import (
    PreparedSummary,
    SummaryClause,
    SummaryMemory,
    prepare_connection,
    prepare_summary,
)
from agent_core.domain.reconsolidation_views import (
    OperationCursor,
    OperationKind,
    OperationState,
    OperationView,
)
from agent_core.ports.determinism import IdFactory

if TYPE_CHECKING:
    from agent_core.adapters.memory.in_memory import InMemoryMemoryStore


class InMemoryReconsolidationStore:
    def __init__(
        self,
        memories: InMemoryMemoryStore,
        ids: IdFactory,
        evidence: ReconsolidationEvidence | None = None,
    ) -> None:
        self._memories = memories
        self._ids = ids
        self._evidence = evidence
        self._transaction = memories._transaction
        self._jobs: MutableMapping[tuple[str, str], ReconsolidationJob] = (
            self._transaction.mapping()
        )
        self._groups: MutableMapping[UUID, ReconsolidationGroup] = self._transaction.mapping()
        self._spend: MutableMapping[UUID, ReconsolidationSpend] = self._transaction.mapping()
        self._audits: list[ReconsolidationAudit] = []

    def _job(self, principal: Principal, token: UUID, now: datetime) -> ReconsolidationJob:
        return check_lease(
            self._jobs.get((principal.tenant_id, principal.principal_id)), principal, token, now
        )

    def _save(self, job: ReconsolidationJob) -> None:
        self._jobs[job.tenant_id, job.principal_id] = job

    def _valid(self, principal: Principal, source: SourceVersion, now: datetime) -> bool:
        record = self._memories._records.get(source.belief_id)
        return (
            record is not None
            and (record.tenant_id, record.principal_id)
            == (principal.tenant_id, principal.principal_id)
            and source.belief_id not in self._memories._erasure_pending
            and self._memories._reconsolidation_index.versions.get(source.belief_id) == source
            and eligible(record, now)
        )

    async def _summary_blocked(self, principal: Principal, content: PreparedSummary) -> bool:
        if self._evidence is None or await self._evidence.claim_rejected(
            principal, tuple(c.text for c in content.clauses)
        ):
            return True
        index = self._memories._reconsolidation_index
        return any(
            (principal.tenant_id, principal.principal_id, key) in index.blocks
            for key in summary_blocks(content)
        )

    async def summary_write_receipt(
        self, principal: Principal, key: str
    ) -> SummaryWriteReceipt | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            return self._memories._reconsolidation_index.summary_receipts.get(
                (principal.tenant_id, principal.principal_id, key)
            )

    async def record_summary_write(
        self, principal: Principal, key: str, receipt: SummaryWriteReceipt
    ) -> None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            if await self.summary_operation(principal, receipt.operation_id) is None:
                raise NotFoundError("memory not found")
            previous = await self.summary_write_receipt(principal, key)
            if previous is not None and previous != receipt:
                raise ConflictError("memory idempotency key was reused")
            self._memories._reconsolidation_index.summary_receipts[
                principal.tenant_id, principal.principal_id, key
            ] = receipt

    async def summary_rejection_targets(
        self, principal: Principal, operation_id: UUID
    ) -> tuple[UUID, ...]:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            operation = await self.summary_operation(principal, operation_id)
            if operation is None:
                return ()
            signatures = set(operation.rejection_signatures)
            return tuple(
                sorted(
                    {
                        operation_id,
                        *(
                            row.id
                            for row in self._memories._reconsolidation_index.operations.values()
                            if isinstance(row, StoredSummary)
                            and (row.plan.tenant_id, row.plan.principal_id)
                            == (principal.tenant_id, principal.principal_id)
                            and bool(signatures & set(row.rejection_signatures))
                        ),
                    }
                )
            )

    async def change_summary(
        self, principal: Principal, operation_id: UUID, action: SummaryAction, now: datetime
    ) -> StoredSummary:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            value = await self.summary_operation(principal, operation_id)
            if value is None:
                raise NotFoundError("memory not found")
            projection = index.summaries.get(operation_id)
            current = (
                None
                if projection is None
                else await self.get_summary(
                    principal,
                    operation_id,
                    now,
                    ceiling=Sensitivity.RESTRICTED,
                    current_scope=projection.content.scope,
                )
            )
            if current is None and not (action == "delete" and value.owner_removed == "untrue"):
                raise NotFoundError("memory not found")
            if action in {"delete", "untrue"} and current is not None:
                for key in summary_blocks(current.content):
                    index.blocks[principal.tenant_id, principal.principal_id, key] = now
            if action in {"delete", "untrue"}:
                for related_id in await self.summary_rejection_targets(principal, operation_id):
                    if related_id == operation_id:
                        continue
                    related = await self.summary_operation(principal, related_id)
                    assert related is not None
                    if related.owner_removed != "delete":
                        index.save(
                            change_summary(
                                related, action, now, await self._memories.next_position()
                            )
                        )
            updated = change_summary(value, action, now, await self._memories.next_position())
            index.save(updated)
            if updated.state == "committed":
                assert projection is not None
                index.summaries[operation_id] = projection.model_copy(
                    update={
                        "revision": updated.revision,
                        "store_position": updated.store_position,
                        "flagged_for_review": not updated.reviewed,
                    }
                )
            return updated

    async def browse_summaries(
        self, principal: Principal, query: MemoryBrowseQuery, now: datetime
    ) -> tuple[SummaryMemory, ...]:
        if (query.tenant_id, query.principal_id) != (principal.tenant_id, principal.principal_id):
            return ()
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            candidates = sorted(
                (
                    row
                    for row in index.summaries.values()
                    if (row.tenant_id, row.principal_id)
                    == (principal.tenant_id, principal.principal_id)
                    and (
                        query.cursor is None
                        or (-row.store_position, row.id) > (-query.cursor[0], query.cursor[1])
                    )
                ),
                key=lambda row: (-row.store_position, row.id),
            )
            result = []
            for row in candidates:
                current = await self.get_summary(
                    principal, row.id, now, ceiling=query.ceiling, current_scope=row.content.scope
                )
                if current is not None and summary_matches(current, query):
                    result.append(current)
                    if len(result) > query.limit:
                        break
            return tuple(result)

    async def operation_page(
        self,
        principal: Principal,
        *,
        kind: OperationKind | None,
        state: OperationState | None,
        before: OperationCursor | None,
        limit: int,
    ) -> tuple[StoredOperation, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("operation page exceeds its bound")
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            values = [
                v
                for v in self._memories._reconsolidation_index.operations.values()
                if (v.plan.tenant_id, v.plan.principal_id)
                == (principal.tenant_id, principal.principal_id)
                and (kind is None or v.kind == kind)
                and (state is None or v.state == state)
                and (before is None or (v.created_at, v.id) < (before.created_at, before.id))
            ]
            return tuple(sorted(values, key=lambda v: (v.created_at, v.id), reverse=True)[:limit])

    async def get_operation(
        self, principal: Principal, operation_id: UUID, now: datetime, *, ceiling: Sensitivity
    ) -> OperationView | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            value = index.operations.get(operation_id)
            if (
                value is None
                or self._evidence is None
                or (value.plan.tenant_id, value.plan.principal_id)
                != (principal.tenant_id, principal.principal_id)
            ):
                return None
            projection = None
            if isinstance(value, StoredSummary) and value.state == "committed":
                try:
                    original = await self._evidence.memories.get(
                        value.plan.member_ids[0], principal
                    )
                    scope = original.scope
                except NotFoundError:
                    scope = ""
                projection = await self.get_summary(
                    principal,
                    operation_id,
                    now,
                    ceiling=Sensitivity.RESTRICTED,
                    current_scope=scope,
                )
                refreshed = await self.summary_operation(principal, operation_id)
                assert refreshed is not None
                value = refreshed
            view, floor = await self._evidence.operation_view(
                principal,
                value,
                now,
                projection=projection,
                versions_match=all(
                    index.version_at(d.source.belief_id, None) == d.source
                    for d in value.plan.dependencies
                ),
            )
            if value.state == "committed" and view.content is None:
                value = value.model_copy(
                    update={
                        "state": "invalidated",
                        "revision": value.revision + 1,
                        "invalidated_at": max(now, value.committed_at),
                        "reason": "source_changed",
                        "store_position": await self._memories.next_position(),
                    }
                )
                index.save(value)
                view = view.model_copy(
                    update={
                        "state": value.state,
                        "revision": value.revision,
                        "invalidated_at": value.invalidated_at,
                        "reason": value.reason,
                    }
                )
            return view if SENSITIVITY_ORDER[floor] <= SENSITIVITY_ORDER[ceiling] else None

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
    ) -> PreparedSummary | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            group = self._groups.get(group_id)
            if group is None or (group.tenant_id, group.principal_id) != (
                principal.tenant_id,
                principal.principal_id,
            ):
                raise NotFoundError("reconsolidation group not found")
            if group.state != "claimed" or group.lease_token != token or group.job_id != job.id:
                raise ConflictError("reconsolidation group lease changed")
            if now >= job.slice_started_at + timedelta(seconds=120):
                raise ConflictError("reconsolidation slice deadline reached")
            sources = selected_sources(group, source_ids)
            if self._evidence is None:
                return None
            prepared = await self._summary(principal, sources, clauses, now, kind=kind)
            if prepared is None:
                return None
            index = self._memories._reconsolidation_index
            identity = evidence_identity(prepared.plan)
            for key in index.dependencies.get(
                (principal.tenant_id, principal.principal_id, sources[0].belief_id), ()
            ):
                previous = index.operations[key]
                if (
                    isinstance(previous, StoredSummary)
                    and previous.kind == kind
                    and previous.evidence_identity == identity
                ):
                    return None
            return prepared

    async def _summary(
        self,
        principal: Principal,
        sources: tuple[SourceVersion, ...],
        clauses: tuple[SummaryClause, ...],
        now: datetime,
        *,
        kind: Literal["summary", "hypothesis"] = "summary",
        formed_at: datetime | None = None,
    ) -> PreparedSummary | None:
        if self._evidence is None:
            return None
        async with self._evidence.people.lock(principal):
            if not all(self._valid(principal, source, now) for source in sources):
                return None
            snapshots = await self._evidence.sources(
                principal,
                tuple((self._memories._records[source.belief_id], source) for source in sources),
            )
            prepared = (
                None
                if snapshots is None
                else prepare_connection(
                    principal, sources, snapshots, clauses, now=now, formed_at=formed_at
                )
                if kind == "hypothesis"
                else prepare_summary(principal, sources, snapshots, clauses, now=now)
            )
            return (
                None
                if prepared is None or await self._summary_blocked(principal, prepared)
                else prepared
            )

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
        if len(member_ids) > 1000 or min_store_position < 0:
            raise ValueError("summary lookup exceeds its bound")
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            owner = (principal.tenant_id, principal.principal_id)
            matched = {
                key for member in member_ids for key in index.dependencies.get((*owner, member), ())
            }
            keys = sorted(
                value.id
                for value in index.operations.values()
                if isinstance(value, StoredSummary)
                and (value.state == "committed" or as_of is not None or known_at is not None)
                and value.store_position > min_store_position
                and (value.plan.tenant_id, value.plan.principal_id) == owner
                and (
                    value.id in matched
                    or (
                        (as_of is not None or known_at is not None)
                        and bool(set(value.plan.member_ids) & set(member_ids))
                    )
                    or (min_store_position > 0 and value.store_position > min_store_position)
                )
            )[:1000]
            result = []
            for key in keys:
                value = await self.get_summary(
                    principal,
                    key,
                    now,
                    ceiling=ceiling,
                    current_scope=current_scope,
                    as_of=as_of,
                    known_at=known_at,
                )
                if value is not None and value.store_position > min_store_position:
                    result.append(value)
            return tuple(result)

    async def update_summary_usage(
        self,
        principal: Principal,
        operation_id: UUID,
        delta: float,
        now: datetime,
        *,
        cited: bool,
    ) -> bool:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            stored = index.summaries.get(operation_id)
            if stored is None:
                return False
            current = await self.get_summary(
                principal,
                operation_id,
                now,
                ceiling=stored.content.sensitivity,
                current_scope=stored.content.scope,
            )
            if current is None:
                return False
            updated = stored.model_copy(
                update={
                    "utility": min(1.0, max(-1.0, current.utility + delta)),
                    "last_used_at": now if cited else current.last_used_at,
                }
            )
            if updated == stored:
                return False
            index.summaries[operation_id] = updated
            return True

    async def summary_operation(
        self, principal: Principal, operation_id: UUID
    ) -> StoredSummary | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            value = self._memories._reconsolidation_index.operations.get(operation_id)
            if (
                value is None
                or not isinstance(value, StoredSummary)
                or (value.plan.tenant_id, value.plan.principal_id)
                != (principal.tenant_id, principal.principal_id)
            ):
                return None
            return value

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
    ) -> StoredConflict | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            fresh = await self.original_input(principal, token, group_id, now)
            if fresh is None:
                return None
            versions = selected_sources(fresh.group, source_ids)
            snapshots = tuple(s for s in fresh.sources if s.version in versions)
            prepared = prepare_summary(
                principal,
                versions,
                snapshots,
                (
                    SummaryClause(
                        text=snapshots[0].record.statement, source_ids=(snapshots[0].record.id,)
                    ),
                ),
                now=now,
            )
            if prepared is None:
                return None
            plan = ConflictPlan(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                member_ids=prepared.plan.member_ids,
                dependencies=prepared.plan.dependencies,
            )
            job = self._job(principal, token, now)
            index = self._memories._reconsolidation_index
            for key in index.dependencies.get(
                (principal.tenant_id, principal.principal_id, plan.member_ids[0]), ()
            ):
                previous = index.operations[key]
                if previous.kind == "conflict" and previous.evidence_identity == evidence_identity(
                    plan
                ):
                    return None
            if job.operations >= 8:
                raise ConflictError("reconsolidation operation budget exhausted")
            value = StoredConflict(
                id=self._ids.new_id(),
                plan=plan,
                group_id=group_id,
                job_id=job.id,
                input_digest=group_digest(versions),
                evidence_identity=evidence_identity(plan),
                model_identity=model_identity,
                created_at=now,
                committed_at=now,
                store_position=await self._memories.next_position(),
            )
            index.save(value)
            self._save(job.model_copy(update={"operations": job.operations + 1}))
            self._groups[group_id] = fresh.group.model_copy(
                update={
                    "state": "committed" if complete_group else "claimed",
                    "reason": "conflict_flagged",
                    "lease_token": None if complete_group else token,
                }
            )
            return value

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
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            prepared = await self.plan_summary(
                principal,
                token,
                group_id,
                expected.clauses,
                now,
                source_ids=expected.plan.member_ids,
                kind=expected.kind,
            )
            if (
                prepared is None
                or (
                    prepared.model_copy(update={"valid_from": expected.valid_from})
                    if expected.kind == "hypothesis"
                    else prepared
                )
                != expected
            ):
                raise ConflictError("summary inputs changed or unavailable")
            job = self._job(principal, token, now)
            if job.operations >= 8:
                raise ConflictError("reconsolidation operation budget exhausted")
            group = self._groups[group_id]
            value = StoredSummary(
                kind=prepared.kind,
                model_identity=model_identity,
                id=self._ids.new_id(),
                plan=prepared.plan,
                group_id=group_id,
                job_id=job.id,
                input_digest=group_digest(tuple(d.source for d in prepared.plan.dependencies)),
                evidence_identity=evidence_identity(prepared.plan),
                rejection_signatures=summary_blocks(prepared),
                created_at=now,
                committed_at=now,
                store_position=await self._memories.next_position(),
            )
            index = self._memories._reconsolidation_index
            index.save(value)
            index.summaries[value.id] = SummaryMemory(
                id=value.id,
                operation_id=value.id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                content=prepared,
                kind=prepared.kind,
                created_at=now,
                store_position=value.store_position,
            )
            self._save(job.model_copy(update={"operations": job.operations + 1}))
            self._groups[group_id] = group.model_copy(
                update={
                    "state": "committed" if complete_group else "claimed",
                    "reason": "inferred" if prepared.kind == "hypothesis" else "summarized",
                    "lease_token": None if complete_group else token,
                }
            )
            return value

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
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            value = index.operations.get(operation_id)
            if (
                value is None
                or not isinstance(value, StoredSummary)
                or (value.plan.tenant_id, value.plan.principal_id)
                != (principal.tenant_id, principal.principal_id)
            ):
                return None
            if value.owner_removed is not None:
                return None
            if (as_of is not None or known_at is not None) and value.kind == "summary":
                instant = as_of or now
                cutoff = min(instant, known_at) if known_at is not None else instant
                if (
                    self._evidence is None
                    or value.committed_at > cutoff
                    or (value.invalidated_at is not None and value.invalidated_at <= cutoff)
                    or any(
                        index.version_at(dep.source.belief_id, known_at) != dep.source
                        for dep in value.plan.dependencies
                    )
                ):
                    return None
                original = index.history[value.id, 1]
                assert isinstance(original, StoredSummary)
                historical = await self._evidence.historical_summary(
                    principal,
                    original,
                    instant,
                    known_at,
                    ceiling=ceiling,
                    current_scope=current_scope,
                )
                if historical is not None and await self._summary_blocked(
                    principal, historical.content
                ):
                    return None
                return controlled_summary(historical, value, current_scope)
            if value.kind == "hypothesis" and (as_of is not None or known_at is not None):
                cutoff = min(as_of or now, known_at or now)
                if value.committed_at > cutoff:
                    return None
            if value.state != "committed":
                return None
            if now < value.committed_at:
                raise ValueError("summary cannot be checked before its commit")
            projection = index.summaries.get(operation_id)
            current = (
                None
                if projection is None
                else await self._summary(
                    principal,
                    tuple(d.source for d in value.plan.dependencies),
                    projection.content.clauses,
                    now,
                    kind=projection.kind,
                    formed_at=projection.content.valid_from,
                )
            )
            if (
                projection is None
                or current is None
                or current != projection.content
                or current.plan != value.plan
            ):
                index.save(
                    value.model_copy(
                        update={
                            "state": "invalidated",
                            "revision": value.revision + 1,
                            "invalidated_at": now,
                            "reason": "source_changed",
                            "store_position": await self._memories.next_position(),
                        }
                    )
                )
                return None
            if SENSITIVITY_ORDER[current.sensitivity] > SENSITIVITY_ORDER[ceiling] or (
                current.portability == Portability.LOCAL and current.scope != current_scope
            ):
                return None
            return controlled_summary(projection, value, current_scope)

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
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            plan = await self.plan_merge(
                principal, token, group_id, now, source_ids=expected.member_ids
            )
            if plan is None or plan != expected:
                raise ConflictError("merge inputs changed or unavailable")
            job = self._job(principal, token, now)
            if job.operations >= 8:
                raise ConflictError("reconsolidation operation budget exhausted")
            group = self._groups[group_id]
            value = StoredMerge(
                id=self._ids.new_id(),
                plan=plan,
                group_id=group_id,
                job_id=job.id,
                input_digest=group_digest(tuple(d.source for d in plan.dependencies)),
                evidence_identity=evidence_identity(plan),
                created_at=now,
                committed_at=now,
                store_position=await self._memories.next_position(),
            )
            self._memories._reconsolidation_index.save(value)
            self._save(job.model_copy(update={"operations": job.operations + 1}))
            self._groups[group_id] = group.model_copy(
                update={
                    "state": "committed" if complete_group else "claimed",
                    "reason": "equivalent",
                    "lease_token": None if complete_group else token,
                }
            )
            return value

    async def get_merge(
        self,
        principal: Principal,
        operation_id: UUID,
        now: datetime,
    ) -> StoredMerge | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            value = index.operations.get(operation_id)
            if (
                value is None
                or value.kind != "merge"
                or (value.plan.tenant_id, value.plan.principal_id)
                != (
                    principal.tenant_id,
                    principal.principal_id,
                )
            ):
                return None
            if now < value.committed_at:
                raise ValueError("merge cannot be checked before its commit")
            if value.state == "committed":
                sources = tuple(d.source for d in value.plan.dependencies)
                current = None
                if self._evidence is not None:
                    async with self._evidence.people.lock(principal):
                        if all(self._valid(principal, source, now) for source in sources):
                            current = await self._evidence.plan(
                                principal,
                                tuple(
                                    (self._memories._records[source.belief_id], source)
                                    for source in sources
                                ),
                                now,
                            )
                if current != value.plan:
                    value = value.model_copy(
                        update={
                            "state": "invalidated",
                            "revision": value.revision + 1,
                            "invalidated_at": now,
                            "reason": "source_changed",
                            "store_position": await self._memories.next_position(),
                        }
                    )
                    index.save(value)
            return value

    async def active_merges(
        self, principal: Principal, member_ids: tuple[UUID, ...], now: datetime
    ) -> tuple[StoredMerge, ...]:
        if len(member_ids) > 1000:
            raise ValueError("membership lookup exceeds 1000 candidates")
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            keys = {
                index.members[(principal.tenant_id, principal.principal_id, key)]
                for key in member_ids
                if (principal.tenant_id, principal.principal_id, key) in index.members
            }
            result = []
            for key in sorted(keys):
                value = await self.get_merge(principal, key, now)
                if value is not None and value.state == "committed":
                    result.append(value)
            return tuple(result)

    async def merges_at(
        self,
        principal: Principal,
        member_ids: tuple[UUID, ...],
        *,
        as_of: datetime,
        known_at: datetime | None,
    ) -> tuple[StoredMerge, ...]:
        if len(member_ids) > 1000:
            raise ValueError("membership lookup exceeds 1000 candidates")
        cutoff = min(as_of, known_at) if known_at is not None else as_of
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            keys = {
                key
                for member in member_ids
                for key in index.historical_members.get(
                    (principal.tenant_id, principal.principal_id, member), frozenset()
                )
            }
            result = []
            for key in sorted(keys):
                value = index.operations[key]
                if value.kind != "merge":
                    continue
                ended = value.undone_at or value.invalidated_at
                if value.committed_at > cutoff or (ended is not None and ended <= cutoff):
                    continue
                original = index.history[key, 1]
                assert original.kind == "merge"
                if self._evidence is None or any(
                    index.version_at(dep.source.belief_id, known_at) != dep.source
                    for dep in original.plan.dependencies
                ):
                    continue
                if await self._evidence.historical(principal, original.plan, as_of, known_at):
                    result.append(original)
            return tuple(result)

    async def merge_members(
        self,
        principal: Principal,
        operation_id: UUID,
        now: datetime,
    ) -> tuple[UUID, ...]:
        value = await self.get_merge(principal, operation_id, now)
        return value.plan.member_ids if value is not None and value.state == "committed" else ()

    async def undo_merge(
        self,
        principal: Principal,
        operation_id: UUID,
        expected_revision: int,
        idempotency_key: str,
        now: datetime,
    ) -> StoredMerge:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            index = self._memories._reconsolidation_index
            owner = (principal.tenant_id, principal.principal_id)
            key = undo_key(principal, idempotency_key)
            value = await self.get_merge(principal, operation_id, now)
            if value is None:
                raise NotFoundError("reconsolidation operation not found")
            prior = index.receipts.get((*owner, key))
            result = undo_merge(
                value,
                principal,
                (),
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                now=now,
                prior_receipt=prior,
            )
            if prior is not None:
                return value
            value = StoredMerge.model_validate(result.operation.model_dump()).model_copy(
                update={
                    "store_position": await self._memories.next_position(),
                    "reason": "owner_undo",
                }
            )
            index.save(value)
            for signature in result.blocked_pairs:
                index.blocks[*owner, signature] = now
            index.receipts[*owner, key] = result.receipt
            return value

    async def original_input(
        self, principal: Principal, token: UUID, group_id: UUID, now: datetime
    ) -> ReconsolidationInput | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            group = self._groups.get(group_id)
            if group is None or (group.tenant_id, group.principal_id) != (
                principal.tenant_id,
                principal.principal_id,
            ):
                raise NotFoundError("reconsolidation group not found")
            if group.state != "claimed" or group.lease_token != token or group.job_id != job.id:
                raise ConflictError("reconsolidation group lease changed")
            if now >= job.slice_started_at + timedelta(seconds=120):
                raise ConflictError("reconsolidation slice deadline reached")
            if self._evidence is None:
                return None
            async with self._evidence.people.lock(principal):
                if not all(self._valid(principal, source, now) for source in group.sources):
                    return None
                return await self._evidence.original_input(
                    principal,
                    group,
                    tuple(
                        (self._memories._records[source.belief_id], source)
                        for source in group.sources
                    ),
                    now,
                )

    async def plan_merge(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        now: datetime,
        *,
        source_ids: tuple[UUID, ...] | None = None,
    ) -> MergePlan | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            group = self._groups.get(group_id)
            if group is None or (group.tenant_id, group.principal_id) != (
                principal.tenant_id,
                principal.principal_id,
            ):
                raise NotFoundError("reconsolidation group not found")
            if group.state != "claimed" or group.lease_token != token or group.job_id != job.id:
                raise ConflictError("reconsolidation group lease changed")
            if now >= job.slice_started_at + timedelta(seconds=120):
                raise ConflictError("reconsolidation slice deadline reached")
            sources = selected_sources(group, source_ids)
            if self._evidence is None:
                return None
            async with self._evidence.people.lock(principal):
                if not all(self._valid(principal, source, now) for source in sources):
                    return None
                plan = await self._evidence.plan(
                    principal,
                    tuple(
                        (self._memories._records[source.belief_id], source) for source in sources
                    ),
                    now,
                )
                index = self._memories._reconsolidation_index
                owner = (principal.tenant_id, principal.principal_id)
                if plan is None or any(
                    (*owner, member) in index.members for member in plan.member_ids
                ):
                    return None
                if any((*owner, key) in index.blocks for key in plan.blocked_pair_signatures):
                    return None
                return plan

    async def claim_due(
        self, principal: Principal, now: datetime, lease_owner: str
    ) -> ReconsolidationJob | None:
        now = utc_now(now)
        if not lease_owner.strip():
            raise ValueError("lease owner is required")
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            owner = (principal.tenant_id, principal.principal_id)
            old = self._jobs.get(owner)
            if old is not None:
                if old.state == "running" and old.lease_expires_at > now:
                    return None
                if old.state == "complete" and old.due_day >= now.date():
                    return None
                for key, spend in self._spend.items():
                    if spend.job_id == old.id and spend.state == "reserved":
                        self._spend[key] = spend.model_copy(
                            update={
                                "state": "unknown",
                                "call_audit": completed_audit(
                                    spend.call_audit,
                                    CallCompletion(reason="recovered_unknown", finished_at=now)
                                    if spend.call_audit is not None
                                    else None,
                                    now,
                                ),
                            }
                        )
                for key, group in self._groups.items():
                    if group.job_id == old.id and group.state == "claimed":
                        exhausted = group.attempts >= 3
                        self._groups[key] = group.model_copy(
                            update={
                                "state": "failed" if exhausted else "pending",
                                "lease_token": None,
                                "reason": "attempts_exhausted" if exhausted else "retry",
                            }
                        )
            index = self._memories._reconsolidation_index
            fresh = old is None or old.state == "complete"
            job = ReconsolidationJob(
                id=self._ids.new_id() if old is None or fresh else old.id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                due_day=now.date() if old is None or fresh else old.due_day,
                generation=1 if old is None else old.generation + int(fresh),
                full_bound=len(index.creations.get(owner, []))
                if old is None or fresh
                else old.full_bound,
                change_bound=len(index.changes.get(owner, []))
                if old is None or fresh
                else old.change_bound,
                full_cursor=0 if old is None or fresh else old.full_cursor,
                change_cursor=0 if old is None else old.change_cursor,
                lease_owner=lease_owner,
                lease_token=self._ids.new_id(),
                lease_expires_at=now + timedelta(seconds=180),
                slice_started_at=now,
                slice_day=now.date(),
                lease_expirations=0
                if old is None
                else old.lease_expirations + int(old.state == "running"),
                revision=1 if old is None else old.revision + 1,
            )
            self._save(job)
            return job

    async def renew(self, principal: Principal, token: UUID, now: datetime) -> ReconsolidationJob:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            if now >= job.slice_started_at + timedelta(seconds=120):
                raise ConflictError("reconsolidation slice deadline reached")
            job = job.model_copy(update={"lease_expires_at": utc_now(now) + timedelta(seconds=180)})
            self._save(job)
            return job

    async def inventory(self, principal: Principal, token: UUID, now: datetime) -> InventoryPage:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            index = self._memories._reconsolidation_index
            owner = (principal.tenant_id, principal.principal_id)
            full_count = min(64, job.full_bound - job.full_cursor)
            changed_count = min(128 - full_count, job.change_bound - job.change_cursor)
            full_count = min(128 - changed_count, job.full_bound - job.full_cursor)
            ids = index.creations.get(owner, [])[job.full_cursor : job.full_cursor + full_count]
            changes = index.changes.get(owner, [])[
                job.change_cursor : job.change_cursor + changed_count
            ]
            candidates = [change.source for change in changes]
            candidates.extend(index.versions[key] for key in ids)
            valid = {s.belief_id: s for s in candidates if self._valid(principal, s, now)}
            return InventoryPage(
                sources=tuple(valid.values()),
                changes=tuple(changes),
                full_cursor=job.full_cursor + full_count,
                change_cursor=job.change_cursor + changed_count,
                inspected=len(candidates),
                excluded=sum(not self._valid(principal, s, now) for s in candidates),
            )

    async def neighbors(
        self, principal: Principal, source: SourceVersion, now: datetime
    ) -> tuple[SourceVersion, ...]:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            if not self._valid(principal, source, now):
                return ()
            anchor = self._memories._records[source.belief_id]
            terms = lexical_query_terms(anchor.subject + " " + anchor.statement)[:64]
            matches: list[tuple[int, int, int, str, SourceVersion]] = []
            for record in self._memories._records.values():
                candidate = self._memories._reconsolidation_index.versions[record.id]
                if (
                    record.id == anchor.id
                    or not self._valid(principal, candidate, now)
                    or not compatible(anchor, record)
                ):
                    continue
                score = neighbor_score(anchor, record, terms)
                if score is not None:
                    matches.append(
                        (
                            -score[0],
                            -score[1],
                            candidate.creation_sequence,
                            str(record.id),
                            candidate,
                        )
                    )
            matches.sort(key=lambda item: item[:4])
            return (source, *(match[4] for match in matches[:31]))

    async def checkpoint(
        self,
        principal: Principal,
        token: UUID,
        page: InventoryPage,
        groups: tuple[tuple[SourceVersion, ...], ...],
        now: datetime,
    ) -> tuple[ReconsolidationGroup, ...]:
        if len(groups) > 4:
            raise ValueError("at most four groups per slice")
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            if now >= job.slice_started_at + timedelta(seconds=120):
                raise ConflictError("reconsolidation slice deadline reached")
            if any(a.lease_token == token for a in self._audits):
                raise ConflictError("slice already checkpointed")
            if page != await self.inventory(principal, token, now):
                raise ConflictError("inventory checkpoint does not match the job")
            pending = sum(
                (g.tenant_id, g.principal_id) == (principal.tenant_id, principal.principal_id)
                and g.state in {"pending", "claimed"}
                for g in self._groups.values()
            )
            prepared: list[ReconsolidationGroup] = []
            seen = {
                g.input_digest
                for g in self._groups.values()
                if (g.tenant_id, g.principal_id) == (principal.tenant_id, principal.principal_id)
            }
            for sources in groups:
                if (
                    len({s.belief_id for s in sources}) != len(sources)
                    or not 2 <= len(sources) <= 32
                ):
                    raise ValueError("group needs two to thirty-two distinct sources")
                if not any(s in page.sources for s in sources):
                    raise ConflictError("group has no inspected anchor")
                if not all(self._valid(principal, s, now) for s in sources):
                    raise ConflictError("source changed before checkpoint")
                first = self._memories._records[sources[0].belief_id]
                if not all(
                    compatible(first, self._memories._records[s.belief_id]) for s in sources
                ):
                    raise ConflictError("group scope mismatch")
                digest = group_digest(sources)
                if digest in seen:
                    continue
                seen.add(digest)
                prepared.append(
                    ReconsolidationGroup(
                        id=self._ids.new_id(),
                        job_id=job.id,
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        sources=tuple(sorted(sources, key=lambda s: str(s.belief_id))),
                        input_digest=digest,
                        created_at=now,
                    )
                )
            if pending + len(prepared) > 256:
                raise ConflictError("reconsolidation queue full")
            for group in prepared:
                self._groups[group.id] = group
            self._save(
                job.model_copy(
                    update={
                        "full_cursor": page.full_cursor,
                        "change_cursor": page.change_cursor,
                        "revision": job.revision + 1,
                    }
                )
            )
            self._transaction.append(
                self._audits,
                ReconsolidationAudit(
                    job_id=job.id,
                    lease_token=token,
                    occurred_at=now,
                    inspected=page.inspected,
                    excluded=page.excluded,
                    selected_groups=len(prepared),
                    not_selected=len(
                        {s.belief_id for s in page.sources}
                        - {s.belief_id for g in prepared for s in g.sources}
                    ),
                ),
            )
            return tuple(prepared)

    async def release(self, principal: Principal, token: UUID, now: datetime) -> ReconsolidationJob:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            if any(g.lease_token == token and g.state == "claimed" for g in self._groups.values()):
                raise ConflictError("claimed groups must finish before release")
            if any(s.lease_token == token and s.state == "reserved" for s in self._spend.values()):
                raise ConflictError("reservations must settle before release")
            pending = any(
                (g.tenant_id, g.principal_id) == (principal.tenant_id, principal.principal_id)
                and g.state == "pending"
                for g in self._groups.values()
            )
            complete = (
                job.full_cursor == job.full_bound
                and job.change_cursor == job.change_bound
                and not pending
            )
            job = job.model_copy(
                update={"state": "complete" if complete else "ready", "revision": job.revision + 1}
            )
            self._save(job)
            return job

    async def claim_group(
        self, principal: Principal, token: UUID, now: datetime
    ) -> ReconsolidationGroup | None:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            if now >= job.slice_started_at + timedelta(seconds=120):
                raise ConflictError("reconsolidation slice deadline reached")
            if job.claimed_groups >= 4:
                return None
            for group in sorted(self._groups.values(), key=lambda g: (g.created_at, str(g.id))):
                if (group.tenant_id, group.principal_id) != (
                    principal.tenant_id,
                    principal.principal_id,
                ) or group.state != "pending":
                    continue
                if not all(self._valid(principal, s, now) for s in group.sources):
                    self._groups[group.id] = group.model_copy(
                        update={"state": "stale", "reason": "source_changed"}
                    )
                    continue
                claimed = group.model_copy(
                    update={
                        "job_id": job.id,
                        "state": "claimed",
                        "lease_token": token,
                        "attempts": group.attempts + 1,
                    }
                )
                self._save(job.model_copy(update={"claimed_groups": job.claimed_groups + 1}))
                self._groups[group.id] = claimed
                return claimed
            return None

    async def finish_group(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        outcome: Literal["no_change", "retry", "committed"],
        now: datetime,
    ) -> ReconsolidationGroup:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            self._job(principal, token, now)
            group = self._groups.get(group_id)
            if group is None or (group.tenant_id, group.principal_id) != (
                principal.tenant_id,
                principal.principal_id,
            ):
                raise NotFoundError("reconsolidation group not found")
            if group.lease_token != token or group.state != "claimed":
                raise ConflictError("reconsolidation group lease lost")
            if outcome == "committed" and group.reason not in {
                "equivalent",
                "summarized",
                "inferred",
                "conflict_flagged",
            }:
                raise ConflictError("group has no committed operations")
            exhausted = outcome == "retry" and group.attempts == 3
            group = group.model_copy(
                update={
                    "state": "failed"
                    if exhausted
                    else "pending"
                    if outcome == "retry"
                    else outcome,
                    "reason": (
                        group.reason
                        if outcome == "committed"
                        else "attempts_exhausted"
                        if exhausted
                        else outcome
                    ),
                    "lease_token": None,
                }
            )
            self._groups[group_id] = group
            return group

    async def reserve(
        self,
        principal: Principal,
        token: UUID,
        request_digest: str,
        maximum_usd: Decimal,
        now: datetime,
        *,
        admission: CallAdmission | None = None,
    ) -> ReconsolidationSpend:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            value = ReconsolidationSpend(
                id=self._ids.new_id(),
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                job_id=job.id,
                lease_token=token,
                day=job.slice_day,
                request_digest=request_digest,
                maximum_usd=maximum_usd,
                charged_usd=maximum_usd,
                call_audit=None
                if admission is None
                else CallAudit(admission=CallAdmission.model_validate(admission.model_dump())),
            )
            if value.call_audit is not None:
                for key in value.call_audit.admission.group_ids:
                    group = self._groups.get(key)
                    if group is None or (
                        group.tenant_id,
                        group.principal_id,
                        group.job_id,
                        group.lease_token,
                        group.state,
                    ) != (principal.tenant_id, principal.principal_id, job.id, token, "claimed"):
                        raise ConflictError("audit group is outside the claimed batch")
            if now >= job.slice_started_at + timedelta(seconds=120):
                raise ConflictError("reconsolidation slice deadline reached")
            if any(
                s.lease_token == token and s.request_digest == request_digest
                for s in self._spend.values()
            ):
                raise ConflictError("request already reserved; retries need a new request identity")
            spent = sum(
                (
                    s.charged_usd
                    for s in self._spend.values()
                    if (s.tenant_id, s.principal_id, s.day)
                    == (principal.tenant_id, principal.principal_id, job.slice_day)
                ),
                Decimal(0),
            )
            if (
                job.requests >= 2
                or job.slice_spent + maximum_usd > SLICE_USD
                or spent + maximum_usd > DAY_USD
            ):
                raise ConflictError("reconsolidation budget exhausted")
            self._spend[value.id] = value
            self._save(
                job.model_copy(
                    update={
                        "requests": job.requests + 1,
                        "slice_spent": job.slice_spent + maximum_usd,
                    }
                )
            )
            return value

    async def get_spend(self, principal: Principal, reservation_id: UUID) -> ReconsolidationSpend:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            value = self._spend.get(reservation_id)
            if value is None or (value.tenant_id, value.principal_id) != (
                principal.tenant_id,
                principal.principal_id,
            ):
                raise NotFoundError("reconsolidation reservation not found")
            return value

    async def record_stage_decision(
        self,
        principal: Principal,
        token: UUID,
        reservation_id: UUID,
        decision: StageDecision,
        now: datetime,
    ) -> ReconsolidationSpend:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            self._job(principal, token, now)
            value = await self.get_spend(principal, reservation_id)
            if value.lease_token != token:
                raise ConflictError("reservation belongs to another lease")
            value = decided_spend(value, decision)
            self._spend[value.id] = value
            return value

    async def settle(
        self,
        principal: Principal,
        token: UUID,
        reservation_id: UUID,
        actual_usd: Decimal | None,
        now: datetime,
        *,
        completion: CallCompletion | None = None,
    ) -> ReconsolidationSpend:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            job = self._job(principal, token, now)
            value = self._spend.get(reservation_id)
            if value is None or (value.tenant_id, value.principal_id) != (
                principal.tenant_id,
                principal.principal_id,
            ):
                raise NotFoundError("reconsolidation reservation not found")
            if value.lease_token != token:
                raise ConflictError("reservation belongs to another lease")
            charged = value.maximum_usd if actual_usd is None else actual_usd
            if not charged.is_finite() or charged < 0 or charged > value.maximum_usd:
                raise ValueError("settlement exceeds admitted reservation")
            state = "unknown" if actual_usd is None else "settled"
            audit = completed_audit(value.call_audit, completion, now)
            if value.state != "reserved":
                if value.state != state or value.charged_usd != charged:
                    raise ConflictError("reservation already settled")
                return value
            settled = ReconsolidationSpend.model_validate(
                {**value.model_dump(), "state": state, "charged_usd": charged, "call_audit": audit}
            )
            self._spend[value.id] = settled
            self._save(
                job.model_copy(
                    update={"slice_spent": job.slice_spent - (value.maximum_usd - charged)}
                )
            )
            return settled
