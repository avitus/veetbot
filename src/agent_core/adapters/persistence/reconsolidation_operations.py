"""Operation writes and reverse invalidation run inside the caller's owner lock/UOW."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, and_, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRevisionRow,
    MemoryRow,
    ReconsolidationDependencyRow,
    ReconsolidationMemberRow,
    ReconsolidationOperationHistoryRow,
    ReconsolidationOperationRow,
    ReconsolidationSummaryRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import review_visible
from agent_core.domain.reconsolidation import SourceVersion
from agent_core.domain.reconsolidation_operations import (
    STORED_OPERATION,
    StoredMerge,
    StoredOperation,
)


async def historical_merges(
    session: AsyncSession,
    principal: Principal,
    member_ids: tuple[UUID, ...],
    as_of: datetime,
    known_at: datetime | None,
) -> tuple[StoredMerge, ...]:
    """Indexed original operation revisions whose interval spans both cutoffs."""
    cutoff = min(as_of, known_at) if known_at is not None else as_of
    operation = ReconsolidationOperationRow
    history = ReconsolidationOperationHistoryRow
    dependencies = ReconsolidationDependencyRow
    ended = func.coalesce(
        cast(operation.payload["undone_at"].astext, DateTime(timezone=True)),
        cast(operation.payload["invalidated_at"].astext, DateTime(timezone=True)),
    )
    payloads = await session.execute(
        select(history.payload, operation.payload)
        .join(
            operation,
            and_(
                operation.tenant_id == history.tenant_id,
                operation.principal_id == history.principal_id,
                operation.id == history.operation_id,
            ),
        )
        .where(
            history.tenant_id == principal.tenant_id,
            history.principal_id == principal.principal_id,
            history.revision == 1,
            operation.payload["kind"].astext == "merge",
            cast(operation.payload["committed_at"].astext, DateTime(timezone=True)) <= cutoff,
            or_(ended.is_(None), ended > cutoff),
            operation.id.in_(
                select(dependencies.operation_id).where(
                    dependencies.tenant_id == principal.tenant_id,
                    dependencies.principal_id == principal.principal_id,
                    dependencies.belief_id.in_(member_ids),
                )
            ),
        )
        .order_by(history.operation_id)
    )
    result = []
    for original, current in payloads:
        current_merge = StoredMerge.model_validate(current)
        if review_visible(current_merge.owner_review, cutoff):
            result.append(
                StoredMerge.model_validate(original).model_copy(
                    update={"owner_review": current_merge.owner_review}
                )
            )
    return tuple(result)


async def source_versions_at(
    session: AsyncSession,
    principal: Principal,
    member_ids: tuple[UUID, ...],
    known_at: datetime | None,
) -> set[SourceVersion]:
    row = MemoryRow if known_at is None else MemoryRevisionRow
    key = MemoryRow.id if known_at is None else MemoryRevisionRow.belief_id
    statement = select(key, row.content_revision, row.creation_sequence).where(
        row.tenant_id == principal.tenant_id,
        row.principal_id == principal.principal_id,
        key.in_(member_ids),
    )
    if known_at is not None:
        statement = (
            statement.where(MemoryRevisionRow.recorded_at <= known_at)
            .distinct(key)
            .order_by(key, MemoryRevisionRow.recorded_at.desc(), MemoryRevisionRow.id.desc())
        )
    rows = await session.execute(statement)
    return {
        SourceVersion(
            belief_id=belief_id,
            content_revision=revision,
            creation_sequence=creation,
        )
        for belief_id, revision, creation in rows
    }


async def position(session: AsyncSession) -> int:
    value = await session.scalar(select(func.nextval("memory_store_position_seq")))
    assert value is not None
    return int(value)


async def save_operation(
    session: AsyncSession, value: StoredOperation, *, new: bool = False
) -> None:
    owner = {"tenant_id": value.plan.tenant_id, "principal_id": value.plan.principal_id}
    payload = value.model_dump(mode="json")
    if new:
        await session.execute(
            pg_insert(ReconsolidationOperationRow).values(
                **owner,
                id=value.id,
                group_id=value.group_id,
                state=value.state,
                revision=value.revision,
                store_position=value.store_position,
                created_at=value.created_at,
                payload=payload,
            )
        )
        for dependency in value.plan.dependencies:
            await session.execute(
                pg_insert(ReconsolidationDependencyRow).values(
                    **owner,
                    operation_id=value.id,
                    belief_id=dependency.source.belief_id,
                    content_revision=dependency.source.content_revision,
                    source_session_id=dependency.source_session_id,
                    source_event_ids=list(dependency.source_event_ids),
                    evidence_at=dependency.evidence_at,
                )
            )
            if value.kind != "merge":
                continue
            await session.execute(
                pg_insert(ReconsolidationMemberRow).values(
                    **owner,
                    operation_id=value.id,
                    canonical_id=value.plan.canonical_id,
                    member_id=dependency.source.belief_id,
                    active=True,
                )
            )
    else:
        await session.execute(
            update(ReconsolidationOperationRow)
            .where(
                ReconsolidationOperationRow.tenant_id == value.plan.tenant_id,
                ReconsolidationOperationRow.principal_id == value.plan.principal_id,
                ReconsolidationOperationRow.id == value.id,
            )
            .values(
                state=value.state,
                revision=value.revision,
                store_position=value.store_position,
                payload=payload,
            )
        )
        await session.execute(
            update(ReconsolidationMemberRow)
            .where(
                ReconsolidationMemberRow.tenant_id == value.plan.tenant_id,
                ReconsolidationMemberRow.principal_id == value.plan.principal_id,
                ReconsolidationMemberRow.operation_id == value.id,
            )
            .values(active=value.state == "committed")
        )
    if value.kind in {"summary", "hypothesis"} and value.state != "committed":
        await session.execute(
            delete(ReconsolidationSummaryRow).where(
                ReconsolidationSummaryRow.tenant_id == value.plan.tenant_id,
                ReconsolidationSummaryRow.principal_id == value.plan.principal_id,
                ReconsolidationSummaryRow.operation_id == value.id,
            )
        )
    await session.execute(
        pg_insert(ReconsolidationOperationHistoryRow).values(
            **owner,
            operation_id=value.id,
            revision=value.revision,
            payload=payload,
        )
    )


async def invalidate_source(
    session: AsyncSession,
    tenant_id: str,
    principal_id: str,
    belief_id: UUID,
    now: datetime,
) -> None:
    operations = ReconsolidationOperationRow
    dependencies = ReconsolidationDependencyRow
    payloads = await session.scalars(
        select(operations.payload)
        .where(
            operations.tenant_id == tenant_id,
            operations.principal_id == principal_id,
            operations.state == "committed",
            operations.id.in_(
                select(dependencies.operation_id).where(
                    dependencies.tenant_id == tenant_id,
                    dependencies.principal_id == principal_id,
                    dependencies.belief_id == belief_id,
                )
            ),
        )
        .order_by(operations.id)
    )
    for payload in payloads:
        value = STORED_OPERATION.validate_python(payload)
        await save_operation(
            session,
            value.model_copy(
                update={
                    "state": "invalidated",
                    "revision": value.revision + 1,
                    "invalidated_at": max(now, value.committed_at),
                    "store_position": await position(session),
                    "reason": "source_changed",
                }
            ),
        )
