"""Exact pgvector search over permitted original revisions (no ANN post-filter)."""

from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Numeric, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.memory_repositories import PostgresMemoryStore, _memory
from agent_core.adapters.persistence.reconsolidation_sources import lock_owner
from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRevisionRow,
    MemoryRow,
    MemoryVectorRow,
    ReconsolidationChangeRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.memory import (
    LIVE_MEMORY_STATUSES,
    SENSITIVITY_ORDER,
    MemoryRecord,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.reconsolidation import SourceChange, SourceVersion, content_signature
from agent_core.domain.semantic_memory import (
    EmbeddingSpace,
    SemanticBounds,
    SemanticHit,
    SemanticPage,
    SemanticSource,
    check_page,
    checked_vector,
    text_digest,
)
from agent_core.ports.determinism import Clock


class PostgresSemanticMemoryIndex:
    def __init__(
        self,
        session: AsyncSession,
        memories: PostgresMemoryStore,
        clock: Clock,
    ) -> None:
        self._session = session
        self._memories = memories
        self._clock = clock

    async def capture_revision(
        self, principal: Principal, source: SourceVersion, space: EmbeddingSpace
    ) -> SemanticSource | None:
        await lock_owner(self._session, principal)
        current = await self._session.scalar(
            select(MemoryRow)
            .where(
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
                MemoryRow.id == source.belief_id,
                ~MemoryRow.erasure_pending,
                MemoryRow.creation_sequence == source.creation_sequence,
            )
            .execution_options(populate_existing=True)
        )
        if current is None:
            return None
        original = await self._session.scalar(
            select(MemoryRevisionRow)
            .where(
                MemoryRevisionRow.tenant_id == principal.tenant_id,
                MemoryRevisionRow.principal_id == principal.principal_id,
                MemoryRevisionRow.belief_id == source.belief_id,
                MemoryRevisionRow.creation_sequence == source.creation_sequence,
                MemoryRevisionRow.content_revision == source.content_revision,
            )
            .order_by(MemoryRevisionRow.id.desc())
            .limit(1)
        )
        if original is None:
            if source.content_revision != current.content_revision:
                return None
            record = _memory(current)
        else:
            record = MemoryRecord.model_validate(original.payload)
        record = record.model_copy(
            update={
                "sensitivity": max(
                    (Sensitivity(current.sensitivity), record.sensitivity),
                    key=SENSITIVITY_ORDER.__getitem__,
                )
            }
        )
        return SemanticSource(
            record=record,
            source=source,
            space=space,
            current_fence=SourceVersion(
                belief_id=current.id,
                content_revision=current.content_revision,
                creation_sequence=current.creation_sequence,
            ),
            content_digest=text_digest(record),
        )

    async def _recapture(
        self, principal: Principal, source: SemanticSource
    ) -> SemanticSource | None:
        if source.current_fence is not None:
            return await self.capture_revision(principal, source.source, source.space)
        return await self.capture(principal, source.source.belief_id, source.space)

    async def capture(
        self, principal: Principal, belief_id: UUID, space: EmbeddingSpace
    ) -> SemanticSource | None:
        await lock_owner(self._session, principal)
        now = self._clock.now()
        row = await self._session.scalar(
            select(MemoryRow)
            .where(
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
                MemoryRow.id == belief_id,
                ~MemoryRow.erasure_pending,
                MemoryRow.status.in_(tuple(s.value for s in LIVE_MEMORY_STATUSES)),
                MemoryRow.valid_from <= now,
                (MemoryRow.valid_to.is_(None) | (MemoryRow.valid_to > now)),
                (MemoryRow.expires_at.is_(None) | (MemoryRow.expires_at > now)),
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        record = _memory(row)
        return SemanticSource(
            record=record,
            source=SourceVersion(
                belief_id=row.id,
                content_revision=row.content_revision,
                creation_sequence=row.creation_sequence,
            ),
            space=space,
            content_digest=text_digest(record),
        )

    async def indexed(self, principal: Principal, source: SemanticSource) -> bool:
        current = await self._recapture(principal, source)
        if (
            current is None
            or current.source != source.source
            or current.current_fence != source.current_fence
        ):
            return False
        return (
            await self._session.scalar(
                select(MemoryVectorRow.belief_id).where(
                    MemoryVectorRow.tenant_id == principal.tenant_id,
                    MemoryVectorRow.principal_id == principal.principal_id,
                    MemoryVectorRow.belief_id == source.source.belief_id,
                    MemoryVectorRow.space_key == source.space.key,
                    MemoryVectorRow.content_revision
                    == (source.current_fence or source.source).content_revision,
                    MemoryVectorRow.historical_revision
                    == (source.source.content_revision if source.current_fence else 0),
                    MemoryVectorRow.creation_sequence == source.source.creation_sequence,
                    MemoryVectorRow.content_digest == current.content_digest,
                )
            )
            is not None
        )

    async def put(
        self, principal: Principal, source: SemanticSource, vector: tuple[float, ...]
    ) -> bool:
        values = checked_vector(vector, source.space)
        current = await self._recapture(principal, source)
        if (
            current is None
            or current.source != source.source
            or current.current_fence != source.current_fence
            or current.content_digest != source.content_digest
            or content_signature(current.record) != content_signature(source.record)
        ):
            return False
        values_dict = {
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
            "belief_id": current.record.id,
            "space_key": source.space.key,
            "space": source.space.model_dump(mode="json"),
            "content_revision": (current.current_fence or current.source).content_revision,
            "historical_revision": current.source.content_revision if current.current_fence else 0,
            "creation_sequence": current.source.creation_sequence,
            "content_digest": current.content_digest,
            "embedding": list(values),
        }
        statement = pg_insert(MemoryVectorRow).values(**values_dict)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    MemoryVectorRow.tenant_id,
                    MemoryVectorRow.principal_id,
                    MemoryVectorRow.belief_id,
                    MemoryVectorRow.space_key,
                    MemoryVectorRow.historical_revision,
                ],
                set_=values_dict,
            )
        )
        return True

    async def search(
        self, query: RecallQuery, space: EmbeddingSpace, vector: tuple[float, ...], *, limit: int
    ) -> list[SemanticHit]:
        values = checked_vector(vector, space)
        check_page(0, 0, limit)
        if query.people_scope is not None:
            await self._session.execute(text("SET LOCAL jit = off"))
        if query.known_at is not None:
            return await self._historical_search(query, space, values, limit)
        # No approximate index: PostgreSQL must evaluate eligibility before LIMIT.
        distance = MemoryVectorRow.embedding.cosine_distance(list(values))
        statement = (
            select(MemoryRow, distance.label("distance"))
            .join(
                MemoryVectorRow,
                (MemoryVectorRow.tenant_id == MemoryRow.tenant_id)
                & (MemoryVectorRow.principal_id == MemoryRow.principal_id)
                & (MemoryVectorRow.belief_id == MemoryRow.id)
                & (MemoryVectorRow.content_revision == MemoryRow.content_revision)
                & (MemoryVectorRow.creation_sequence == MemoryRow.creation_sequence),
            )
            .where(
                *self._memories.current_predicates(query),
                MemoryVectorRow.space_key == space.key,
                MemoryVectorRow.historical_revision
                == (MemoryRow.content_revision if query.as_of is not None else 0),
            )
            .order_by(func.round(distance.cast(Numeric), 7), MemoryRow.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return [
            SemanticHit(record=_memory(row), similarity=max(-1.0, min(1.0, 1 - float(score))))
            for row, score in (await self._session.execute(statement)).all()
        ]

    async def _historical_search(
        self,
        query: RecallQuery,
        space: EmbeddingSpace,
        values: tuple[float, ...],
        limit: int,
    ) -> list[SemanticHit]:
        latest, predicates = self._memories.historical_predicates(query)
        distance = MemoryVectorRow.embedding.cosine_distance(list(values))
        statement = (
            select(latest.c.payload, MemoryRow.sensitivity, distance.label("distance"))
            .select_from(latest)
            .join(
                MemoryRow,
                (MemoryRow.id == latest.c.belief_id)
                & (MemoryRow.tenant_id == latest.c.tenant_id)
                & (MemoryRow.principal_id == latest.c.principal_id),
            )
            .join(
                MemoryVectorRow,
                (MemoryVectorRow.belief_id == MemoryRow.id)
                & (MemoryVectorRow.tenant_id == MemoryRow.tenant_id)
                & (MemoryVectorRow.principal_id == MemoryRow.principal_id)
                & (MemoryVectorRow.content_revision == MemoryRow.content_revision)
                & (MemoryVectorRow.creation_sequence == latest.c.creation_sequence)
                & (MemoryVectorRow.historical_revision == latest.c.content_revision),
            )
            .where(*predicates, MemoryVectorRow.space_key == space.key)
            .order_by(func.round(distance.cast(Numeric), 7), MemoryRow.id)
            .limit(limit)
        )
        hits = []
        for data, sensitivity, score in (await self._session.execute(statement)).all():
            record = MemoryRecord.model_validate(data)
            record = record.model_copy(
                update={
                    "sensitivity": max(
                        (record.sensitivity, Sensitivity(sensitivity)),
                        key=SENSITIVITY_ORDER.__getitem__,
                    )
                }
            )
            hits.append(
                SemanticHit(record=record, similarity=max(-1.0, min(1.0, 1 - float(score))))
            )
        return hits

    async def bounds(
        self, principal: Principal, *, include_history: bool = False
    ) -> SemanticBounds:
        owner = await lock_owner(self._session, principal)
        return SemanticBounds(
            creation=owner.change_count if include_history else owner.creation_count,
            change=owner.change_count,
        )

    async def inventory(
        self,
        principal: Principal,
        *,
        after: int,
        through: int,
        limit: int = 64,
        include_history: bool = False,
    ) -> SemanticPage:
        check_page(after, through, limit)
        if include_history:
            page = await self.changes(principal, after=after, through=through, limit=limit)
            return SemanticPage(
                sources=tuple(c.source for c in page.changes if c.reason != "erased"),
                cursor=page.cursor,
            )
        bound = min(through, (await self.bounds(principal)).creation)
        if after > bound:
            raise ValueError("cursor exceeds owner inventory")
        end = min(bound, after + limit)
        rows = (
            await self._session.scalars(
                select(MemoryRow)
                .where(
                    MemoryRow.tenant_id == principal.tenant_id,
                    MemoryRow.principal_id == principal.principal_id,
                    MemoryRow.creation_sequence > after,
                    MemoryRow.creation_sequence <= end,
                    ~MemoryRow.erasure_pending,
                )
                .order_by(MemoryRow.creation_sequence)
            )
        ).all()
        return SemanticPage(
            sources=tuple(
                SourceVersion(
                    belief_id=row.id,
                    content_revision=row.content_revision,
                    creation_sequence=row.creation_sequence,
                )
                for row in rows
            ),
            cursor=end,
        )

    async def changes(
        self, principal: Principal, *, after: int, through: int, limit: int = 64
    ) -> SemanticPage:
        check_page(after, through, limit)
        bound = min(through, (await self.bounds(principal)).change)
        if after > bound:
            raise ValueError("cursor exceeds owner journal")
        end = min(bound, after + limit)
        rows = (
            await self._session.scalars(
                select(ReconsolidationChangeRow)
                .where(
                    ReconsolidationChangeRow.tenant_id == principal.tenant_id,
                    ReconsolidationChangeRow.principal_id == principal.principal_id,
                    ReconsolidationChangeRow.sequence > after,
                    ReconsolidationChangeRow.sequence <= end,
                )
                .order_by(ReconsolidationChangeRow.sequence)
            )
        ).all()
        return SemanticPage(
            changes=tuple(
                SourceChange(
                    sequence=row.sequence,
                    source=SourceVersion(
                        belief_id=row.belief_id,
                        content_revision=row.content_revision,
                        creation_sequence=row.creation_sequence,
                    ),
                    reason=cast(Literal["created", "changed", "erased"], row.reason),
                )
                for row in rows
            ),
            cursor=end,
        )
