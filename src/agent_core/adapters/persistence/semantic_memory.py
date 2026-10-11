"""Exact pgvector search over current, permitted originals (no ANN post-filter)."""

from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Numeric, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.memory_repositories import PostgresMemoryStore, _memory
from agent_core.adapters.persistence.reconsolidation_sources import lock_owner
from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRow,
    MemoryVectorRow,
    ReconsolidationChangeRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.memory import LIVE_MEMORY_STATUSES, RecallQuery
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
    def __init__(self, session: AsyncSession, memories: PostgresMemoryStore, clock: Clock) -> None:
        self._session = session
        self._memories = memories
        self._clock = clock

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

    async def put(
        self, principal: Principal, source: SemanticSource, vector: tuple[float, ...]
    ) -> bool:
        values = checked_vector(vector, source.space)
        current = await self.capture(principal, source.source.belief_id, source.space)
        if (
            current is None
            or current.source != source.source
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
            "content_revision": current.source.content_revision,
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
        if query.known_at is not None or query.as_of is not None or query.people_scope is not None:
            return []
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
                *self._memories.current_predicates(query), MemoryVectorRow.space_key == space.key
            )
            .order_by(func.round(distance.cast(Numeric), 7), MemoryRow.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return [
            SemanticHit(record=_memory(row), similarity=max(-1.0, min(1.0, 1 - float(score))))
            for row, score in (await self._session.execute(statement)).all()
        ]

    async def bounds(self, principal: Principal) -> SemanticBounds:
        owner = await lock_owner(self._session, principal)
        return SemanticBounds(creation=owner.creation_count, change=owner.change_count)

    async def inventory(
        self, principal: Principal, *, after: int, through: int, limit: int = 64
    ) -> SemanticPage:
        check_page(after, through, limit)
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
