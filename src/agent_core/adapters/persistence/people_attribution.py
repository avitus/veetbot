"""Transactional reverse attribution and original-memory revision fences."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, and_, any_, bindparam, exists, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.memory_repositories import _memory
from agent_core.adapters.persistence.reconsolidation_sources import append_change, lock_owner
from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRevisionRow,
    MemoryRow,
    PeopleHeadRow,
    PeopleLinkRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.reconsolidation_attribution import AttributionFootprint, attribution_targets


async def footprint_targets(
    session: AsyncSession, principal: Principal, footprints: list[AttributionFootprint]
) -> tuple[set[UUID], set[tuple[UUID, int]]]:
    source_ids = {
        item.source_id
        for item in footprints
        if item.source_id is not None and item.session_id is None
    }
    if source_ids:
        values = (
            await session.scalars(
                select(PeopleHeadRow.memory_attribution).where(
                    PeopleHeadRow.tenant_id == principal.tenant_id,
                    PeopleHeadRow.principal_id == principal.principal_id,
                    PeopleHeadRow.id
                    == any_(bindparam(None, sorted(source_ids), type_=ARRAY(PGUUID(as_uuid=True)))),
                    PeopleHeadRow.memory_attribution.is_not(None),
                )
            )
        ).all()
        footprints = [
            *footprints,
            *(AttributionFootprint.model_validate(value) for value in values),
        ]
    return attribution_targets(footprints)


async def affected_attribution(
    session: AsyncSession, principal: Principal, roots: list[UUID]
) -> tuple[set[UUID], set[tuple[UUID, int]]]:
    if not roots:
        return set(), set()
    affected = (
        select(PeopleHeadRow.id)
        .where(
            PeopleHeadRow.tenant_id == principal.tenant_id,
            PeopleHeadRow.principal_id == principal.principal_id,
            PeopleHeadRow.id == any_(bindparam(None, roots, type_=ARRAY(PGUUID(as_uuid=True)))),
        )
        .cte("attribution_dependents", recursive=True)
    )
    affected = affected.union(
        select(PeopleLinkRow.entity_id)
        .join(
            affected,
            PeopleLinkRow.target_id == affected.c.id,
        )
        .join(
            PeopleHeadRow,
            and_(
                PeopleHeadRow.tenant_id == PeopleLinkRow.tenant_id,
                PeopleHeadRow.principal_id == PeopleLinkRow.principal_id,
                PeopleHeadRow.id == PeopleLinkRow.entity_id,
                PeopleHeadRow.revision == PeopleLinkRow.revision,
            ),
        )
        .where(
            PeopleLinkRow.tenant_id == principal.tenant_id,
            PeopleLinkRow.principal_id == principal.principal_id,
        )
    )
    values = (
        await session.scalars(
            select(PeopleHeadRow.memory_attribution).where(
                PeopleHeadRow.tenant_id == principal.tenant_id,
                PeopleHeadRow.principal_id == principal.principal_id,
                PeopleHeadRow.id.in_(select(affected.c.id)),
                PeopleHeadRow.memory_attribution.is_not(None),
            )
        )
    ).all()
    return await footprint_targets(
        session, principal, [AttributionFootprint.model_validate(value) for value in values]
    )


async def advance_attribution_revisions(
    session: AsyncSession,
    principal: Principal,
    belief_ids: set[UUID],
    events: set[tuple[UUID, int]],
    now: datetime,
    *,
    all_originals: bool = False,
) -> None:
    if not all_originals and not belief_ids and not events:
        return
    owner = await lock_owner(session, principal)
    pairs = sorted(events)
    event_rows = (
        func.unnest(
            bindparam(None, [pair[0] for pair in pairs], type_=ARRAY(PGUUID(as_uuid=True))),
            bindparam(None, [pair[1] for pair in pairs], type_=ARRAY(BigInteger())),
        )
        .table_valued("session_id", "event_sequence")
        .render_derived("attribution_events")
    )
    predicate = or_(
        MemoryRow.id
        == any_(bindparam(None, sorted(belief_ids), type_=ARRAY(PGUUID(as_uuid=True)))),
        exists(
            select(1)
            .select_from(event_rows)
            .where(
                MemoryRow.source_session_id == event_rows.c.session_id,
                MemoryRow.source_event_ids.contains(
                    func.jsonb_build_array(event_rows.c.event_sequence)
                ),
            )
        ),
    )
    rows = (
        await session.scalars(
            select(MemoryRow)
            .where(
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
                ~MemoryRow.erasure_pending,
                true() if all_originals else predicate,
            )
            .order_by(MemoryRow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    for row in rows:
        revision = row.content_revision + 1
        await append_change(session, owner, row.id, revision, row.creation_sequence, "changed", now)
        await session.execute(
            update(MemoryRow).where(MemoryRow.id == row.id).values(content_revision=revision)
        )
        await session.execute(
            pg_insert(MemoryRevisionRow).values(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                belief_id=row.id,
                recorded_at=now,
                payload=_memory(row).model_dump(mode="json"),
                content_revision=revision,
                creation_sequence=row.creation_sequence,
            )
        )
