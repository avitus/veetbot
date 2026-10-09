"""PostgreSQL reconsolidation maintenance; methods never own the transaction."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import and_, case, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.memory.reconsolidation_evidence import ReconsolidationEvidence
from agent_core.adapters.persistence.memory_repositories import _memory
from agent_core.adapters.persistence.reconsolidation_mappers import (
    group_to_domain,
    group_values,
    job_to_domain,
    job_values,
    spend_to_domain,
    spend_values,
)
from agent_core.adapters.persistence.reconsolidation_operations import (
    historical_merges,
    position,
    save_operation,
    source_versions_at,
)
from agent_core.adapters.persistence.reconsolidation_sources import lock_owner
from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRow,
    ReconsolidationAuditRow,
    ReconsolidationBlockRow,
    ReconsolidationChangeRow,
    ReconsolidationDayRow,
    ReconsolidationDependencyRow,
    ReconsolidationGroupRow,
    ReconsolidationJobRow,
    ReconsolidationMemberRow,
    ReconsolidationMemoryWriteRow,
    ReconsolidationOperationHistoryRow,
    ReconsolidationOperationRow,
    ReconsolidationSpendRow,
    ReconsolidationSummaryRow,
    ReconsolidationUndoRow,
)
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
    ReconsolidationGroup,
    ReconsolidationJob,
    ReconsolidationSpend,
    SourceChange,
    SourceVersion,
    StageDecision,
    check_lease,
    compatible,
    completed_audit,
    decided_spend,
    eligible,
    group_digest,
    selected_sources,
    utc_now,
)
from agent_core.domain.reconsolidation_inputs import ReconsolidationInput
from agent_core.domain.reconsolidation_merge import MergePlan, MergeUndoReceipt, undo_merge
from agent_core.domain.reconsolidation_operations import (
    STORED_OPERATION,
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


def _version(row: MemoryRow) -> SourceVersion:
    return SourceVersion(
        belief_id=row.id,
        content_revision=row.content_revision,
        creation_sequence=row.creation_sequence,
    )


class PostgresReconsolidationStore:
    def __init__(
        self, session: AsyncSession, ids: IdFactory, evidence: ReconsolidationEvidence | None = None
    ) -> None:
        self._session = session
        self._ids = ids
        self._evidence = evidence

    async def _job(self, principal: Principal, token: UUID, now: datetime) -> ReconsolidationJob:
        owner = await lock_owner(self._session, principal)
        row = await self._session.scalar(
            select(ReconsolidationJobRow)
            .where(
                ReconsolidationJobRow.id == owner.current_job_id,
                ReconsolidationJobRow.tenant_id == principal.tenant_id,
                ReconsolidationJobRow.principal_id == principal.principal_id,
            )
            .execution_options(populate_existing=True)
        )
        return check_lease(None if row is None else job_to_domain(row), principal, token, now)

    async def _save(self, job: ReconsolidationJob) -> None:
        await self._session.execute(
            update(ReconsolidationJobRow)
            .where(
                ReconsolidationJobRow.id == job.id,
                ReconsolidationJobRow.tenant_id == job.tenant_id,
                ReconsolidationJobRow.principal_id == job.principal_id,
            )
            .values(**job_values(job))
        )

    async def _sources(
        self, principal: Principal, sources: tuple[SourceVersion, ...], now: datetime
    ) -> list[MemoryRow]:
        if not sources:
            return []
        rows = (
            await self._session.scalars(
                select(MemoryRow)
                .where(
                    MemoryRow.tenant_id == principal.tenant_id,
                    MemoryRow.principal_id == principal.principal_id,
                    MemoryRow.id.in_([s.belief_id for s in sources]),
                    ~MemoryRow.erasure_pending,
                )
                .order_by(MemoryRow.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        expected = {s.belief_id: s for s in sources}
        return [r for r in rows if _version(r) == expected[r.id] and eligible(_memory(r), now)]

    async def _summary_blocked(self, principal: Principal, content: PreparedSummary) -> bool:
        if self._evidence is None or await self._evidence.claim_rejected(
            principal, tuple(c.text for c in content.clauses)
        ):
            return True
        row = ReconsolidationBlockRow
        return (
            await self._session.scalar(
                select(row.signature)
                .where(
                    row.tenant_id == principal.tenant_id,
                    row.principal_id == principal.principal_id,
                    row.signature.in_(summary_blocks(content)),
                )
                .limit(1)
            )
            is not None
        )

    async def summary_write_receipt(
        self, principal: Principal, key: str
    ) -> SummaryWriteReceipt | None:
        row = ReconsolidationMemoryWriteRow
        payload = await self._session.scalar(
            select(row.payload).where(
                row.tenant_id == principal.tenant_id,
                row.principal_id == principal.principal_id,
                row.key_digest == key,
            )
        )
        return None if payload is None else SummaryWriteReceipt.model_validate(payload)

    async def record_summary_write(
        self, principal: Principal, key: str, receipt: SummaryWriteReceipt
    ) -> None:
        await lock_owner(self._session, principal)
        if await self.summary_operation(principal, receipt.operation_id) is None:
            raise NotFoundError("memory not found")
        previous = await self.summary_write_receipt(principal, key)
        if previous is not None:
            if previous != receipt:
                raise ConflictError("memory idempotency key was reused")
            return
        await self._session.execute(
            pg_insert(ReconsolidationMemoryWriteRow).values(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                key_digest=key,
                operation_id=receipt.operation_id,
                payload=receipt.model_dump(mode="json"),
            )
        )

    async def summary_rejection_targets(
        self, principal: Principal, operation_id: UUID
    ) -> tuple[UUID, ...]:
        await lock_owner(self._session, principal)
        operation = await self.summary_operation(principal, operation_id)
        if operation is None:
            return ()
        row = ReconsolidationOperationRow
        keys = await self._session.scalars(
            select(row.id)
            .where(
                row.tenant_id == principal.tenant_id,
                row.principal_id == principal.principal_id,
                row.payload["kind"].astext.in_(("summary", "hypothesis")),
                or_(
                    row.id == operation_id,
                    *(
                        row.payload["rejection_signatures"].contains([key])
                        for key in operation.rejection_signatures
                    ),
                ),
            )
            .order_by(row.id)
        )
        return tuple(keys)

    async def change_summary(
        self, principal: Principal, operation_id: UUID, action: SummaryAction, now: datetime
    ) -> StoredSummary:
        await lock_owner(self._session, principal)
        value = await self.summary_operation(principal, operation_id)
        if value is None:
            raise NotFoundError("memory not found")
        row = ReconsolidationSummaryRow
        payload = await self._session.scalar(
            select(row.payload).where(
                row.tenant_id == principal.tenant_id,
                row.principal_id == principal.principal_id,
                row.operation_id == operation_id,
            )
        )
        projection = None if payload is None else SummaryMemory.model_validate(payload)
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
                await self._session.execute(
                    pg_insert(ReconsolidationBlockRow)
                    .values(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        signature=key,
                        reason="owner_summary_rejection",
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )
        if action in {"delete", "untrue"}:
            for related_id in await self.summary_rejection_targets(principal, operation_id):
                if related_id == operation_id:
                    continue
                related = await self.summary_operation(principal, related_id)
                assert related is not None
                if related.owner_removed != "delete":
                    await save_operation(
                        self._session,
                        change_summary(related, action, now, await position(self._session)),
                    )
        updated = change_summary(value, action, now, await position(self._session))
        await save_operation(self._session, updated)
        if updated.state == "committed":
            assert projection is not None
            projection = projection.model_copy(
                update={
                    "revision": updated.revision,
                    "store_position": updated.store_position,
                    "flagged_for_review": not updated.reviewed,
                }
            )
            await self._session.execute(
                update(row)
                .where(
                    row.tenant_id == principal.tenant_id,
                    row.principal_id == principal.principal_id,
                    row.operation_id == operation_id,
                )
                .values(payload=projection.model_dump(mode="json"))
            )
        return updated

    async def browse_summaries(
        self, principal: Principal, query: MemoryBrowseQuery, now: datetime
    ) -> tuple[SummaryMemory, ...]:
        if (query.tenant_id, query.principal_id) != (principal.tenant_id, principal.principal_id):
            return ()
        await lock_owner(self._session, principal)
        row = ReconsolidationOperationRow
        projection = ReconsolidationSummaryRow
        before = query.cursor
        result = []
        while True:
            statement = (
                select(projection.payload)
                .join(
                    row,
                    and_(
                        row.tenant_id == projection.tenant_id,
                        row.principal_id == projection.principal_id,
                        row.id == projection.operation_id,
                    ),
                )
                .where(
                    row.tenant_id == principal.tenant_id, row.principal_id == principal.principal_id
                )
            )
            if before is not None:
                statement = statement.where(
                    or_(
                        row.store_position < before[0],
                        and_(row.store_position == before[0], row.id > before[1]),
                    )
                )
            payloads = (
                await self._session.scalars(
                    statement.order_by(row.store_position.desc(), row.id).limit(100)
                )
            ).all()
            for payload in payloads:
                stored = SummaryMemory.model_validate(payload)
                before = (stored.store_position, stored.id)
                current = await self.get_summary(
                    principal,
                    stored.id,
                    now,
                    ceiling=query.ceiling,
                    current_scope=stored.content.scope,
                )
                if current is not None and summary_matches(current, query):
                    result.append(current)
                    if len(result) > query.limit:
                        return tuple(result)
            if len(payloads) < 100:
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
        row = ReconsolidationOperationRow
        statement = select(row.payload).where(
            row.tenant_id == principal.tenant_id, row.principal_id == principal.principal_id
        )
        if kind is not None:
            statement = statement.where(row.payload["kind"].astext == kind)
        if state is not None:
            statement = statement.where(row.state == state)
        if before is not None:
            statement = statement.where(
                or_(
                    row.created_at < before.created_at,
                    and_(row.created_at == before.created_at, row.id < before.id),
                )
            )
        payloads = await self._session.scalars(
            statement.order_by(row.created_at.desc(), row.id.desc()).limit(limit)
        )
        return tuple(STORED_OPERATION.validate_python(payload) for payload in payloads)

    async def get_operation(
        self, principal: Principal, operation_id: UUID, now: datetime, *, ceiling: Sensitivity
    ) -> OperationView | None:
        await lock_owner(self._session, principal)
        row = ReconsolidationOperationRow
        payload = await self._session.scalar(
            select(row.payload).where(
                row.tenant_id == principal.tenant_id,
                row.principal_id == principal.principal_id,
                row.id == operation_id,
            )
        )
        if payload is None or self._evidence is None:
            return None
        value = STORED_OPERATION.validate_python(payload)
        versions = await source_versions_at(self._session, principal, value.plan.member_ids, None)
        projection = None
        if isinstance(value, StoredSummary) and value.state == "committed":
            try:
                original = await self._evidence.memories.get(value.plan.member_ids[0], principal)
                scope = original.scope
            except NotFoundError:
                scope = ""
            projection = await self.get_summary(
                principal, operation_id, now, ceiling=Sensitivity.RESTRICTED, current_scope=scope
            )
            refreshed = await self.summary_operation(principal, operation_id)
            assert refreshed is not None
            value = refreshed
        view, floor = await self._evidence.operation_view(
            principal,
            value,
            now,
            projection=projection,
            versions_match=versions == {d.source for d in value.plan.dependencies},
        )
        if value.state == "committed" and view.content is None:
            value = value.model_copy(
                update={
                    "state": "invalidated",
                    "revision": value.revision + 1,
                    "invalidated_at": max(now, value.committed_at),
                    "reason": "source_changed",
                    "store_position": await position(self._session),
                }
            )
            await save_operation(self._session, value)
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
        job = await self._job(principal, token, now)
        row = await self._session.scalar(
            select(ReconsolidationGroupRow)
            .where(
                ReconsolidationGroupRow.id == group_id,
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError("reconsolidation group not found")
        group = group_to_domain(row)
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
        operation, dependency = ReconsolidationOperationRow, ReconsolidationDependencyRow
        previous = await self._session.scalar(
            select(operation.id)
            .where(
                operation.tenant_id == principal.tenant_id,
                operation.principal_id == principal.principal_id,
                operation.payload["kind"].astext == kind,
                operation.payload["evidence_identity"].astext == evidence_identity(prepared.plan),
                operation.id.in_(
                    select(dependency.operation_id).where(
                        dependency.tenant_id == principal.tenant_id,
                        dependency.principal_id == principal.principal_id,
                        dependency.belief_id == sources[0].belief_id,
                    )
                ),
            )
            .limit(1)
        )
        return prepared if previous is None else None

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
            rows = await self._sources(principal, sources, now)
            if len(rows) != len(sources):
                return None
            snapshots = await self._evidence.sources(
                principal, tuple((_memory(row), _version(row)) for row in rows)
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
        await lock_owner(self._session, principal)
        operation, dependency = ReconsolidationOperationRow, ReconsolidationDependencyRow
        linked = operation.id.in_(
            select(dependency.operation_id).where(
                dependency.tenant_id == principal.tenant_id,
                dependency.principal_id == principal.principal_id,
                dependency.belief_id.in_(member_ids),
            )
        )
        keys = (
            await self._session.scalars(
                select(operation.id)
                .where(
                    operation.tenant_id == principal.tenant_id,
                    operation.principal_id == principal.principal_id,
                    or_(
                        operation.state == "committed",
                        literal(as_of is not None or known_at is not None),
                    ),
                    operation.payload["kind"].astext.in_(("summary", "hypothesis")),
                    or_(
                        linked,
                        (operation.store_position > min_store_position)
                        if min_store_position
                        else literal(False),
                    ),
                    operation.store_position > min_store_position,
                )
                .order_by(operation.id)
                .limit(1000)
            )
        ).all()
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
        await lock_owner(self._session, principal)
        row = await self._session.scalar(
            select(ReconsolidationSummaryRow).where(
                ReconsolidationSummaryRow.tenant_id == principal.tenant_id,
                ReconsolidationSummaryRow.principal_id == principal.principal_id,
                ReconsolidationSummaryRow.operation_id == operation_id,
            )
        )
        if row is None:
            return False
        stored = SummaryMemory.model_validate(row.payload)
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
        row.payload = updated.model_dump(mode="json")
        return True

    async def summary_operation(
        self, principal: Principal, operation_id: UUID
    ) -> StoredSummary | None:
        payload = await self._session.scalar(
            select(ReconsolidationOperationRow.payload).where(
                ReconsolidationOperationRow.tenant_id == principal.tenant_id,
                ReconsolidationOperationRow.principal_id == principal.principal_id,
                ReconsolidationOperationRow.id == operation_id,
                ReconsolidationOperationRow.payload["kind"].astext.in_(("summary", "hypothesis")),
            )
        )
        return None if payload is None else StoredSummary.model_validate(payload)

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
        job = await self._job(principal, token, now)
        operation, dependency = ReconsolidationOperationRow, ReconsolidationDependencyRow
        previous = await self._session.scalar(
            select(operation.id)
            .where(
                operation.tenant_id == principal.tenant_id,
                operation.principal_id == principal.principal_id,
                operation.payload["kind"].astext == "conflict",
                operation.payload["evidence_identity"].astext == evidence_identity(plan),
                operation.id.in_(
                    select(dependency.operation_id).where(
                        dependency.tenant_id == principal.tenant_id,
                        dependency.principal_id == principal.principal_id,
                        dependency.belief_id == plan.member_ids[0],
                    )
                ),
            )
            .limit(1)
        )
        if previous is not None:
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
            store_position=await position(self._session),
        )
        await save_operation(self._session, value, new=True)
        await self._save(job.model_copy(update={"operations": job.operations + 1}))
        await self._session.execute(
            update(ReconsolidationGroupRow)
            .where(
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
                ReconsolidationGroupRow.id == group_id,
            )
            .values(
                state="committed" if complete_group else "claimed",
                reason="conflict_flagged",
                lease_token=None if complete_group else token,
            )
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
        job = await self._job(principal, token, now)
        if job.operations >= 8:
            raise ConflictError("reconsolidation operation budget exhausted")
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
            store_position=await position(self._session),
        )
        await save_operation(self._session, value, new=True)
        projection = SummaryMemory(
            id=value.id,
            operation_id=value.id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            content=prepared,
            kind=prepared.kind,
            created_at=now,
            store_position=value.store_position,
        )
        await self._session.execute(
            pg_insert(ReconsolidationSummaryRow).values(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                operation_id=value.id,
                payload=projection.model_dump(mode="json"),
            )
        )
        await self._save(job.model_copy(update={"operations": job.operations + 1}))
        await self._session.execute(
            update(ReconsolidationGroupRow)
            .where(
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
                ReconsolidationGroupRow.id == group_id,
            )
            .values(
                state="committed" if complete_group else "claimed",
                reason="inferred" if prepared.kind == "hypothesis" else "summarized",
                lease_token=None if complete_group else token,
            )
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
        try:
            await lock_owner(self._session, principal)
        except NotFoundError:
            # This optional reader returns absence for an inaccessible owner,
            # without attempting a foreign-tenant write or poisoning the UOW.
            return None
        payload = await self._session.scalar(
            select(ReconsolidationOperationRow.payload).where(
                ReconsolidationOperationRow.tenant_id == principal.tenant_id,
                ReconsolidationOperationRow.principal_id == principal.principal_id,
                ReconsolidationOperationRow.id == operation_id,
            )
        )
        if payload is None or payload["kind"] not in {"summary", "hypothesis"}:
            return None
        value = StoredSummary.model_validate(payload)
        if value.owner_removed is not None:
            return None
        if (as_of is not None or known_at is not None) and value.kind == "summary":
            instant = as_of or now
            cutoff = min(instant, known_at) if known_at is not None else instant
            if (
                self._evidence is None
                or value.committed_at > cutoff
                or (value.invalidated_at is not None and value.invalidated_at <= cutoff)
                or await source_versions_at(
                    self._session, principal, value.plan.member_ids, known_at
                )
                != {dep.source for dep in value.plan.dependencies}
            ):
                return None
            history = await self._session.scalar(
                select(ReconsolidationOperationHistoryRow.payload).where(
                    ReconsolidationOperationHistoryRow.tenant_id == principal.tenant_id,
                    ReconsolidationOperationHistoryRow.principal_id == principal.principal_id,
                    ReconsolidationOperationHistoryRow.operation_id == value.id,
                    ReconsolidationOperationHistoryRow.revision == 1,
                )
            )
            if history is None:
                return None
            historical = await self._evidence.historical_summary(
                principal,
                StoredSummary.model_validate(history),
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
        content = await self._session.scalar(
            select(ReconsolidationSummaryRow.payload).where(
                ReconsolidationSummaryRow.tenant_id == principal.tenant_id,
                ReconsolidationSummaryRow.principal_id == principal.principal_id,
                ReconsolidationSummaryRow.operation_id == operation_id,
            )
        )
        projection = None if content is None else SummaryMemory.model_validate(content)
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
            await save_operation(
                self._session,
                value.model_copy(
                    update={
                        "state": "invalidated",
                        "revision": value.revision + 1,
                        "invalidated_at": now,
                        "reason": "source_changed",
                        "store_position": await position(self._session),
                    }
                ),
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
        plan = await self.plan_merge(
            principal, token, group_id, now, source_ids=expected.member_ids
        )
        if plan is None or plan != expected:
            raise ConflictError("merge inputs changed or unavailable")
        job = await self._job(principal, token, now)
        if job.operations >= 8:
            raise ConflictError("reconsolidation operation budget exhausted")
        value = StoredMerge(
            id=self._ids.new_id(),
            plan=plan,
            group_id=group_id,
            job_id=job.id,
            input_digest=group_digest(tuple(d.source for d in plan.dependencies)),
            evidence_identity=evidence_identity(plan),
            created_at=now,
            committed_at=now,
            store_position=await position(self._session),
        )
        await save_operation(self._session, value, new=True)
        await self._save(job.model_copy(update={"operations": job.operations + 1}))
        await self._session.execute(
            update(ReconsolidationGroupRow)
            .where(
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
                ReconsolidationGroupRow.id == group_id,
            )
            .values(
                state="committed" if complete_group else "claimed",
                reason="equivalent",
                lease_token=None if complete_group else token,
            )
        )
        return value

    async def get_merge(
        self,
        principal: Principal,
        operation_id: UUID,
        now: datetime,
    ) -> StoredMerge | None:
        await lock_owner(self._session, principal)
        payload = await self._session.scalar(
            select(ReconsolidationOperationRow.payload).where(
                ReconsolidationOperationRow.tenant_id == principal.tenant_id,
                ReconsolidationOperationRow.principal_id == principal.principal_id,
                ReconsolidationOperationRow.id == operation_id,
            )
        )
        if payload is None or payload["kind"] != "merge":
            return None
        value = StoredMerge.model_validate(payload)
        if now < value.committed_at:
            raise ValueError("merge cannot be checked before its commit")
        if value.state == "committed":
            sources = tuple(d.source for d in value.plan.dependencies)
            current = None
            if self._evidence is not None:
                async with self._evidence.people.lock(principal):
                    rows = await self._sources(principal, sources, now)
                    if len(rows) == len(sources):
                        current = await self._evidence.plan(
                            principal, tuple((_memory(row), _version(row)) for row in rows), now
                        )
            if current != value.plan:
                value = value.model_copy(
                    update={
                        "state": "invalidated",
                        "revision": value.revision + 1,
                        "invalidated_at": now,
                        "reason": "source_changed",
                        "store_position": await position(self._session),
                    }
                )
                await save_operation(self._session, value)
        return value

    async def active_merges(
        self, principal: Principal, member_ids: tuple[UUID, ...], now: datetime
    ) -> tuple[StoredMerge, ...]:
        if len(member_ids) > 1000:
            raise ValueError("membership lookup exceeds 1000 candidates")
        if not member_ids:
            return ()
        await lock_owner(self._session, principal)
        keys = await self._session.scalars(
            select(ReconsolidationMemberRow.operation_id)
            .where(
                ReconsolidationMemberRow.tenant_id == principal.tenant_id,
                ReconsolidationMemberRow.principal_id == principal.principal_id,
                ReconsolidationMemberRow.member_id.in_(member_ids),
                ReconsolidationMemberRow.active,
            )
            .distinct()
            .order_by(ReconsolidationMemberRow.operation_id)
        )
        result = []
        for key in keys:
            value = await self.get_merge(principal, key, now)
            if value is not None and value.state == "committed":
                result.append(value)
        return tuple(result)

    async def merge_members(
        self,
        principal: Principal,
        operation_id: UUID,
        now: datetime,
    ) -> tuple[UUID, ...]:
        value = await self.get_merge(principal, operation_id, now)
        return value.plan.member_ids if value is not None and value.state == "committed" else ()

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
        if not member_ids or self._evidence is None:
            return ()
        await lock_owner(self._session, principal)
        result = []
        for value in await historical_merges(self._session, principal, member_ids, as_of, known_at):
            versions = await source_versions_at(
                self._session, principal, value.plan.member_ids, known_at
            )
            if versions != {dep.source for dep in value.plan.dependencies}:
                continue
            if await self._evidence.historical(principal, value.plan, as_of, known_at):
                result.append(value)
        return tuple(result)

    async def undo_merge(
        self,
        principal: Principal,
        operation_id: UUID,
        expected_revision: int,
        idempotency_key: str,
        now: datetime,
    ) -> StoredMerge:
        key = undo_key(principal, idempotency_key)
        value = await self.get_merge(principal, operation_id, now)
        if value is None:
            raise NotFoundError("reconsolidation operation not found")
        payload = await self._session.scalar(
            select(ReconsolidationUndoRow.payload).where(
                ReconsolidationUndoRow.tenant_id == principal.tenant_id,
                ReconsolidationUndoRow.principal_id == principal.principal_id,
                ReconsolidationUndoRow.key_digest == key,
            )
        )
        prior = None if payload is None else MergeUndoReceipt.model_validate(payload)
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
                "store_position": await position(self._session),
                "reason": "owner_undo",
            }
        )
        await save_operation(self._session, value)
        owner = {"tenant_id": principal.tenant_id, "principal_id": principal.principal_id}
        for signature in result.blocked_pairs:
            await self._session.execute(
                pg_insert(ReconsolidationBlockRow)
                .values(
                    **owner,
                    signature=signature,
                    reason="owner_undo",
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )
        await self._session.execute(
            pg_insert(ReconsolidationUndoRow).values(
                **owner,
                key_digest=key,
                operation_id=value.id,
                payload=result.receipt.model_dump(mode="json"),
            )
        )
        return value

    async def original_input(
        self, principal: Principal, token: UUID, group_id: UUID, now: datetime
    ) -> ReconsolidationInput | None:
        job = await self._job(principal, token, now)
        row = await self._session.scalar(
            select(ReconsolidationGroupRow)
            .where(
                ReconsolidationGroupRow.id == group_id,
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError("reconsolidation group not found")
        group = group_to_domain(row)
        if group.state != "claimed" or group.lease_token != token or group.job_id != job.id:
            raise ConflictError("reconsolidation group lease changed")
        if now >= job.slice_started_at + timedelta(seconds=120):
            raise ConflictError("reconsolidation slice deadline reached")
        if self._evidence is None:
            return None
        async with self._evidence.people.lock(principal):
            rows = await self._sources(principal, group.sources, now)
            if len(rows) != len(group.sources):
                return None
            return await self._evidence.original_input(
                principal, group, tuple((_memory(row), _version(row)) for row in rows), now
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
        job = await self._job(principal, token, now)
        row = await self._session.scalar(
            select(ReconsolidationGroupRow)
            .where(
                ReconsolidationGroupRow.id == group_id,
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError("reconsolidation group not found")
        group = group_to_domain(row)
        if group.state != "claimed" or group.lease_token != token or group.job_id != job.id:
            raise ConflictError("reconsolidation group lease changed")
        if now >= job.slice_started_at + timedelta(seconds=120):
            raise ConflictError("reconsolidation slice deadline reached")
        sources = selected_sources(group, source_ids)
        if self._evidence is None:
            return None
        async with self._evidence.people.lock(principal):
            rows = await self._sources(principal, sources, now)
            if len(rows) != len(sources):
                return None
            plan = await self._evidence.plan(
                principal, tuple((_memory(row), _version(row)) for row in rows), now
            )
            if plan is None:
                return None
            occupied = await self._session.scalar(
                select(ReconsolidationMemberRow.operation_id)
                .where(
                    ReconsolidationMemberRow.tenant_id == principal.tenant_id,
                    ReconsolidationMemberRow.principal_id == principal.principal_id,
                    ReconsolidationMemberRow.member_id.in_(plan.member_ids),
                    ReconsolidationMemberRow.active,
                )
                .limit(1)
            )
            blocked = await self._session.scalar(
                select(ReconsolidationBlockRow.signature)
                .where(
                    ReconsolidationBlockRow.tenant_id == principal.tenant_id,
                    ReconsolidationBlockRow.principal_id == principal.principal_id,
                    ReconsolidationBlockRow.signature.in_(plan.blocked_pair_signatures),
                )
                .limit(1)
            )
            return plan if occupied is None and blocked is None else None

    async def claim_due(
        self, principal: Principal, now: datetime, lease_owner: str
    ) -> ReconsolidationJob | None:
        now = utc_now(now)
        if not lease_owner.strip():
            raise ValueError("lease owner is required")
        owner = await lock_owner(self._session, principal)
        row = (
            await self._session.get(ReconsolidationJobRow, owner.current_job_id)
            if owner.current_job_id
            else None
        )
        old = None if row is None else job_to_domain(row)
        if old is not None:
            if old.state == "running" and old.lease_expires_at > now:
                return None
            if old.state == "complete" and old.due_day >= now.date():
                return None
            abandoned = await self._session.scalars(
                select(ReconsolidationSpendRow)
                .where(
                    ReconsolidationSpendRow.job_id == old.id,
                    ReconsolidationSpendRow.state == "reserved",
                )
                .execution_options(populate_existing=True)
            )
            for abandoned_row in abandoned:
                spend = spend_to_domain(abandoned_row)
                recovered = spend.model_copy(
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
                await self._session.execute(
                    update(ReconsolidationSpendRow)
                    .where(ReconsolidationSpendRow.id == spend.id)
                    .values(**spend_values(recovered))
                )
            await self._session.execute(
                update(ReconsolidationGroupRow)
                .where(
                    ReconsolidationGroupRow.job_id == old.id,
                    ReconsolidationGroupRow.state == "claimed",
                )
                .values(
                    state=case((ReconsolidationGroupRow.attempts >= 3, "failed"), else_="pending"),
                    lease_token=None,
                    reason=case(
                        (ReconsolidationGroupRow.attempts >= 3, "attempts_exhausted"), else_="retry"
                    ),
                )
            )
        fresh = old is None or old.state == "complete"
        job = ReconsolidationJob(
            id=self._ids.new_id() if old is None or fresh else old.id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            due_day=now.date() if old is None or fresh else old.due_day,
            generation=1 if old is None else old.generation + int(fresh),
            full_bound=owner.creation_count if old is None or fresh else old.full_bound,
            change_bound=owner.change_count if old is None or fresh else old.change_bound,
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
        if fresh:
            await self._session.execute(pg_insert(ReconsolidationJobRow).values(**job_values(job)))
        else:
            await self._save(job)
        owner.current_job_id = job.id
        await self._session.flush()
        return job

    async def renew(self, principal: Principal, token: UUID, now: datetime) -> ReconsolidationJob:
        job = await self._job(principal, token, now)
        if now >= job.slice_started_at + timedelta(seconds=120):
            raise ConflictError("reconsolidation slice deadline reached")
        job = job.model_copy(update={"lease_expires_at": utc_now(now) + timedelta(seconds=180)})
        await self._save(job)
        return job

    async def inventory(self, principal: Principal, token: UUID, now: datetime) -> InventoryPage:
        job = await self._job(principal, token, now)
        full_count = min(64, job.full_bound - job.full_cursor)
        changed_count = min(128 - full_count, job.change_bound - job.change_cursor)
        full_count = min(128 - changed_count, job.full_bound - job.full_cursor)
        rows = (
            await self._session.scalars(
                select(MemoryRow)
                .where(
                    MemoryRow.tenant_id == principal.tenant_id,
                    MemoryRow.principal_id == principal.principal_id,
                    MemoryRow.creation_sequence > job.full_cursor,
                    MemoryRow.creation_sequence <= job.full_cursor + full_count,
                )
                .order_by(MemoryRow.creation_sequence, MemoryRow.id)
                .limit(128)
                .execution_options(populate_existing=True)
            )
        ).all()
        full_valid = [
            _version(r) for r in rows if not r.erasure_pending and eligible(_memory(r), now)
        ]
        changes = (
            await self._session.scalars(
                select(ReconsolidationChangeRow)
                .where(
                    ReconsolidationChangeRow.tenant_id == principal.tenant_id,
                    ReconsolidationChangeRow.principal_id == principal.principal_id,
                    ReconsolidationChangeRow.sequence > job.change_cursor,
                    ReconsolidationChangeRow.sequence <= job.change_cursor + changed_count,
                )
                .order_by(ReconsolidationChangeRow.sequence)
                .limit(128)
            )
        ).all()
        values = tuple(
            SourceChange(
                sequence=c.sequence,
                source=SourceVersion(
                    belief_id=c.belief_id,
                    content_revision=c.content_revision,
                    creation_sequence=c.creation_sequence,
                ),
                reason=cast(Literal["created", "changed", "erased"], c.reason),
            )
            for c in changes
        )
        # Last revision per source is the only one eligible for selection.
        wanted = {c.source.belief_id: c.source for c in values}
        current = await self._sources(principal, tuple(wanted.values()), now)
        valid_changed = {_version(r) for r in current}
        changed_sources = [c.source for c in values if c.source in valid_changed]
        combined = {s.belief_id: s for s in [*changed_sources, *full_valid]}
        return InventoryPage(
            sources=tuple(combined.values()),
            changes=values,
            full_cursor=job.full_cursor + full_count,
            change_cursor=job.change_cursor + changed_count,
            inspected=full_count + changed_count,
            excluded=full_count - len(full_valid) + changed_count - len(changed_sources),
        )

    async def neighbors(
        self, principal: Principal, source: SourceVersion, now: datetime
    ) -> tuple[SourceVersion, ...]:
        found = await self._sources(principal, (source,), now)
        if not found:
            return ()
        anchor = _memory(found[0])
        if not compatible(anchor, anchor):
            return (source,)
        terms = lexical_query_terms(anchor.subject + " " + anchor.statement)[:64]
        # Every admission predicate precedes LIMIT; candidates stay owner/scope bound.
        overlap = sum(
            (
                case(
                    (
                        func.to_tsvector(
                            "simple", MemoryRow.subject + " " + MemoryRow.statement
                        ).op("@@")(func.plainto_tsquery("simple", term)),
                        1,
                    ),
                    else_=0,
                )
                for term in terms
            ),
            literal(0),
        )
        rows = (
            await self._session.scalars(
                select(MemoryRow)
                .where(
                    MemoryRow.tenant_id == principal.tenant_id,
                    MemoryRow.principal_id == principal.principal_id,
                    MemoryRow.id != source.belief_id,
                    ~MemoryRow.erasure_pending,
                    MemoryRow.status.in_(("active", "provisional")),
                    MemoryRow.derivation == "direct",
                    MemoryRow.sensitivity.in_(("public", "internal")),
                    MemoryRow.valid_from <= now,
                    or_(MemoryRow.expires_at.is_(None), MemoryRow.expires_at > now),
                    or_(MemoryRow.valid_to.is_(None), MemoryRow.valid_to > now),
                    MemoryRow.scope == anchor.scope,
                    *([MemoryRow.portability == "portable"] if anchor.scope == "user" else []),
                    or_(
                        MemoryRow.subject == anchor.subject,
                        *[
                            func.to_tsvector(
                                "simple", MemoryRow.subject + " " + MemoryRow.statement
                            ).op("@@")(func.plainto_tsquery("simple", term))
                            for term in terms
                        ],
                    ),
                )
                .order_by(
                    (MemoryRow.subject == anchor.subject).desc(),
                    overlap.desc(),
                    MemoryRow.creation_sequence,
                    MemoryRow.id,
                )
                .limit(31)
            )
        ).all()
        return (source, *(_version(r) for r in rows))

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
        job = await self._job(principal, token, now)
        if now >= job.slice_started_at + timedelta(seconds=120):
            raise ConflictError("reconsolidation slice deadline reached")
        prior = await self._session.scalar(
            select(ReconsolidationAuditRow.lease_token).where(
                ReconsolidationAuditRow.tenant_id == principal.tenant_id,
                ReconsolidationAuditRow.principal_id == principal.principal_id,
                ReconsolidationAuditRow.lease_token == token,
            )
        )
        if prior is not None:
            raise ConflictError("slice already checkpointed")
        if page != await self.inventory(principal, token, now):
            raise ConflictError("inventory checkpoint does not match the job")
        pending = await self._session.scalar(
            select(func.count())
            .select_from(ReconsolidationGroupRow)
            .where(
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
                ReconsolidationGroupRow.state.in_(("pending", "claimed")),
            )
        )
        prepared: list[ReconsolidationGroup] = []
        seen: set[str] = set()
        for sources in groups:
            if len({s.belief_id for s in sources}) != len(sources) or not 2 <= len(sources) <= 32:
                raise ValueError("group needs two to thirty-two distinct sources")
            if not any(s in page.sources for s in sources):
                raise ConflictError("group has no inspected anchor")
            rows = await self._sources(principal, sources, now)
            if len(rows) != len(sources):
                raise ConflictError("source changed before checkpoint")
            first = _memory(rows[0])
            if not all(compatible(first, _memory(row)) for row in rows):
                raise ConflictError("group scope mismatch")
            digest = group_digest(sources)
            existing = await self._session.scalar(
                select(ReconsolidationGroupRow.id).where(
                    ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                    ReconsolidationGroupRow.principal_id == principal.principal_id,
                    ReconsolidationGroupRow.policy == job.policy,
                    ReconsolidationGroupRow.input_digest == digest,
                )
            )
            if digest in seen or existing is not None:
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
        if (pending or 0) + len(prepared) > 256:
            raise ConflictError("reconsolidation queue full")
        for group in prepared:
            await self._session.execute(
                pg_insert(ReconsolidationGroupRow).values(**group_values(group))
            )
        await self._save(
            job.model_copy(
                update={
                    "full_cursor": page.full_cursor,
                    "change_cursor": page.change_cursor,
                    "revision": job.revision + 1,
                }
            )
        )
        await self._session.execute(
            pg_insert(ReconsolidationAuditRow).values(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
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
            )
        )
        return tuple(prepared)

    async def release(self, principal: Principal, token: UUID, now: datetime) -> ReconsolidationJob:
        job = await self._job(principal, token, now)
        claimed = await self._session.scalar(
            select(ReconsolidationGroupRow.id)
            .where(
                ReconsolidationGroupRow.lease_token == token,
                ReconsolidationGroupRow.state == "claimed",
            )
            .limit(1)
        )
        reserved = await self._session.scalar(
            select(ReconsolidationSpendRow.id)
            .where(
                ReconsolidationSpendRow.lease_token == token,
                ReconsolidationSpendRow.state == "reserved",
            )
            .limit(1)
        )
        if claimed is not None or reserved is not None:
            raise ConflictError("claimed groups and reservations must finish before release")
        pending = await self._session.scalar(
            select(ReconsolidationGroupRow.id)
            .where(
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
                ReconsolidationGroupRow.state == "pending",
            )
            .limit(1)
        )
        complete = (
            job.full_cursor == job.full_bound
            and job.change_cursor == job.change_bound
            and pending is None
        )
        job = job.model_copy(
            update={"state": "complete" if complete else "ready", "revision": job.revision + 1}
        )
        await self._save(job)
        return job

    async def claim_group(
        self, principal: Principal, token: UUID, now: datetime
    ) -> ReconsolidationGroup | None:
        job = await self._job(principal, token, now)
        if now >= job.slice_started_at + timedelta(seconds=120):
            raise ConflictError("reconsolidation slice deadline reached")
        if job.claimed_groups >= 4:
            return None
        rows = (
            await self._session.scalars(
                select(ReconsolidationGroupRow)
                .where(
                    ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                    ReconsolidationGroupRow.principal_id == principal.principal_id,
                    ReconsolidationGroupRow.state == "pending",
                )
                .order_by(ReconsolidationGroupRow.created_at, ReconsolidationGroupRow.id)
                .limit(256)
            )
        ).all()
        for row in rows:
            group = group_to_domain(row)
            if len(await self._sources(principal, group.sources, now)) != len(group.sources):
                await self._session.execute(
                    update(ReconsolidationGroupRow)
                    .where(ReconsolidationGroupRow.id == group.id)
                    .values(state="stale", reason="source_changed")
                )
                continue
            group = group.model_copy(
                update={
                    "job_id": job.id,
                    "state": "claimed",
                    "lease_token": token,
                    "attempts": group.attempts + 1,
                }
            )
            await self._session.execute(
                update(ReconsolidationGroupRow)
                .where(ReconsolidationGroupRow.id == group.id)
                .values(**group_values(group))
            )
            await self._save(job.model_copy(update={"claimed_groups": job.claimed_groups + 1}))
            return group
        return None

    async def finish_group(
        self,
        principal: Principal,
        token: UUID,
        group_id: UUID,
        outcome: Literal["no_change", "retry", "committed"],
        now: datetime,
    ) -> ReconsolidationGroup:
        await self._job(principal, token, now)
        row = await self._session.scalar(
            select(ReconsolidationGroupRow)
            .where(
                ReconsolidationGroupRow.id == group_id,
                ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                ReconsolidationGroupRow.principal_id == principal.principal_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError("reconsolidation group not found")
        group = group_to_domain(row)
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
                "state": "failed" if exhausted else "pending" if outcome == "retry" else outcome,
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
        await self._session.execute(
            update(ReconsolidationGroupRow)
            .where(ReconsolidationGroupRow.id == group.id)
            .values(**group_values(group))
        )
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
        job = await self._job(principal, token, now)
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
            keys = value.call_audit.admission.group_ids
            found = await self._session.scalars(
                select(ReconsolidationGroupRow.id).where(
                    ReconsolidationGroupRow.id.in_(keys),
                    ReconsolidationGroupRow.tenant_id == principal.tenant_id,
                    ReconsolidationGroupRow.principal_id == principal.principal_id,
                    ReconsolidationGroupRow.job_id == job.id,
                    ReconsolidationGroupRow.lease_token == token,
                    ReconsolidationGroupRow.state == "claimed",
                )
            )
            if set(found) != set(keys):
                raise ConflictError("audit group is outside the claimed batch")
        if now >= job.slice_started_at + timedelta(seconds=120):
            raise ConflictError("reconsolidation slice deadline reached")
        prior = await self._session.scalar(
            select(ReconsolidationSpendRow.id).where(
                ReconsolidationSpendRow.lease_token == token,
                ReconsolidationSpendRow.request_digest == request_digest,
            )
        )
        if prior is not None:
            raise ConflictError("request already reserved; retries need a new request identity")
        await self._session.execute(
            pg_insert(ReconsolidationDayRow)
            .values(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                day=job.slice_day,
            )
            .on_conflict_do_nothing()
        )
        day = await self._session.scalar(
            select(ReconsolidationDayRow)
            .where(
                ReconsolidationDayRow.tenant_id == principal.tenant_id,
                ReconsolidationDayRow.principal_id == principal.principal_id,
                ReconsolidationDayRow.day == job.slice_day,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert day is not None
        if (
            job.requests >= 2
            or job.slice_spent + maximum_usd > SLICE_USD
            or day.charged_usd + maximum_usd > DAY_USD
        ):
            raise ConflictError("reconsolidation budget exhausted")
        await self._session.execute(
            pg_insert(ReconsolidationSpendRow).values(**spend_values(value))
        )
        day.charged_usd += maximum_usd
        await self._save(
            job.model_copy(
                update={"requests": job.requests + 1, "slice_spent": job.slice_spent + maximum_usd}
            )
        )
        await self._session.flush()
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
        job = await self._job(principal, token, now)
        row = await self._session.scalar(
            select(ReconsolidationSpendRow)
            .where(
                ReconsolidationSpendRow.id == reservation_id,
                ReconsolidationSpendRow.tenant_id == principal.tenant_id,
                ReconsolidationSpendRow.principal_id == principal.principal_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError("reconsolidation reservation not found")
        value = spend_to_domain(row)
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
        await self._session.execute(
            update(ReconsolidationSpendRow)
            .where(ReconsolidationSpendRow.id == value.id)
            .values(**spend_values(settled))
        )
        difference = value.maximum_usd - charged
        await self._session.execute(
            update(ReconsolidationDayRow)
            .where(
                ReconsolidationDayRow.tenant_id == principal.tenant_id,
                ReconsolidationDayRow.principal_id == principal.principal_id,
                ReconsolidationDayRow.day == value.day,
            )
            .values(charged_usd=ReconsolidationDayRow.charged_usd - difference)
        )
        await self._save(job.model_copy(update={"slice_spent": job.slice_spent - difference}))
        return settled

    async def get_spend(self, principal: Principal, reservation_id: UUID) -> ReconsolidationSpend:
        row = await self._session.scalar(
            select(ReconsolidationSpendRow)
            .where(
                ReconsolidationSpendRow.id == reservation_id,
                ReconsolidationSpendRow.tenant_id == principal.tenant_id,
                ReconsolidationSpendRow.principal_id == principal.principal_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError("reconsolidation reservation not found")
        return spend_to_domain(row)

    async def record_stage_decision(
        self,
        principal: Principal,
        token: UUID,
        reservation_id: UUID,
        decision: StageDecision,
        now: datetime,
    ) -> ReconsolidationSpend:
        # Refuse foreign/missing receipts before a lease lookup can create an
        # owner guard. Re-read under that guard to serialize concurrent decisions.
        await self.get_spend(principal, reservation_id)
        await self._job(principal, token, now)
        value = await self.get_spend(principal, reservation_id)
        if value.lease_token != token:
            raise ConflictError("reservation belongs to another lease")
        value = decided_spend(value, decision)
        await self._session.execute(
            update(ReconsolidationSpendRow)
            .where(ReconsolidationSpendRow.id == value.id)
            .values(**spend_values(value))
        )
        return value
