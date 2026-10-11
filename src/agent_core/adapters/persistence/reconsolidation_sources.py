"""Owner-serialized sequence allocation shared by memory writes and scans."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.reconsolidation_operations import invalidate_source
from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRow,
    ReconsolidationChangeRow,
    ReconsolidationOwnerRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError


async def lock_owner(session: AsyncSession, principal: Principal) -> ReconsolidationOwnerRow:
    tenant = await session.scalar(select(func.current_setting("agent_core.tenant_id", True)))
    if tenant != principal.tenant_id:
        raise NotFoundError("memory owner not found")
    await session.execute(
        pg_insert(ReconsolidationOwnerRow)
        .values(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
        )
        .on_conflict_do_nothing()
    )
    row = await session.scalar(
        select(ReconsolidationOwnerRow)
        .where(
            ReconsolidationOwnerRow.tenant_id == principal.tenant_id,
            ReconsolidationOwnerRow.principal_id == principal.principal_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def append_change(
    session: AsyncSession,
    owner: ReconsolidationOwnerRow,
    belief_id: UUID,
    revision: int,
    creation: int,
    reason: str,
    now: datetime,
) -> None:
    if reason != "created":
        await invalidate_source(session, owner.tenant_id, owner.principal_id, belief_id, now)
    owner.change_count += 1
    await session.execute(
        pg_insert(ReconsolidationChangeRow).values(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            sequence=owner.change_count,
            belief_id=belief_id,
            content_revision=revision,
            creation_sequence=creation,
            reason=reason,
        )
    )
    await session.flush()


async def erase_sources(
    session: AsyncSession, principal: Principal, belief_ids: list[UUID], now: datetime
) -> None:
    owner = await lock_owner(session, principal)
    rows = (
        await session.scalars(
            select(MemoryRow)
            .where(
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
                MemoryRow.id.in_(belief_ids),
                ~MemoryRow.erasure_pending,
            )
            .order_by(MemoryRow.id)
        )
    ).all()
    for row in rows:
        await append_change(
            session, owner, row.id, row.content_revision + 1, row.creation_sequence, "erased", now
        )
        await session.execute(
            update(MemoryRow)
            .where(MemoryRow.id == row.id)
            .values(content_revision=row.content_revision + 1)
        )
