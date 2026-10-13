"""Exact derived-vector search with transactional original-memory fences."""

from uuid import UUID

from agent_core.adapters.memory.in_memory import InMemoryMemoryStore
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import SENSITIVITY_ORDER, RecallQuery, Sensitivity
from agent_core.domain.reconsolidation import SourceVersion, content_signature
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


def capture_query(principal: Principal, belief_id: UUID) -> RecallQuery:
    return RecallQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        current_scope="user",
        include_ids=(belief_id,),
        subjects=[],
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        budget_tokens=1,
        max_items=1,
        min_score=0,
    )


class InMemorySemanticMemoryIndex:
    def __init__(self, memories: InMemoryMemoryStore) -> None:
        self._memories = memories
        self._index = memories._reconsolidation_index

    async def capture_revision(
        self, principal: Principal, source: SourceVersion, space: EmbeddingSpace
    ) -> SemanticSource | None:
        async with self._index.transaction.owner((principal.tenant_id, principal.principal_id)):
            try:
                current = await self._memories.get(source.belief_id, principal)
            except NotFoundError:
                return None
            fence = self._index.versions[current.id]
            if source.creation_sequence != fence.creation_sequence or not any(
                version == source for _, version in self._index.version_history.get(current.id, [])
            ):
                return None
            # Attribution changes advance the source revision without changing
            # the stored text. Recover the last text version at that real source
            # revision, never a later correction or an invented revision.
            original = next(
                (
                    record
                    for _, record, version in reversed(self._memories._history.get(current.id, []))
                    if version.creation_sequence == source.creation_sequence
                    and version.content_revision <= source.content_revision
                ),
                None,
            )
            if original is None:
                return None
            record = original.model_copy(
                update={
                    "sensitivity": max(
                        (current.sensitivity, original.sensitivity),
                        key=SENSITIVITY_ORDER.__getitem__,
                    )
                }
            )
            return SemanticSource(
                record=record,
                source=source,
                current_fence=fence,
                space=space,
                content_digest=text_digest(record),
            )

    async def _recapture(
        self, principal: Principal, source: SemanticSource
    ) -> SemanticSource | None:
        if source.current_fence is not None:
            return await self.capture_revision(principal, source.source, source.space)
        return await self.capture(principal, source.source.belief_id, source.space)

    @staticmethod
    def _slot(source: SemanticSource) -> str:
        if source.current_fence is not None:
            return f"{source.space.key}:{source.source.content_revision}"
        return source.space.key

    async def capture(
        self, principal: Principal, belief_id: UUID, space: EmbeddingSpace
    ) -> SemanticSource | None:
        async with self._index.transaction.owner((principal.tenant_id, principal.principal_id)):
            try:
                record = await self._memories.get(belief_id, principal)
            except NotFoundError:
                return None
            query = capture_query(principal, belief_id).model_copy(
                update={"current_scope": record.scope}
            )
            if not await self._memories._eligible_records(query, lexical=False):
                return None
            return SemanticSource(
                record=record,
                source=self._index.versions[belief_id],
                space=space,
                content_digest=text_digest(record),
            )

    async def indexed(self, principal: Principal, source: SemanticSource) -> bool:
        async with self._index.transaction.owner((principal.tenant_id, principal.principal_id)):
            current = await self._recapture(principal, source)
            entry = self._index.semantic_vectors.get(
                (principal.tenant_id, principal.principal_id, source.source.belief_id), {}
            ).get(self._slot(source))
            return (
                current is not None
                and current.source == source.source
                and current.current_fence == source.current_fence
                and entry is not None
                and entry[0].source == current.source
                and entry[0].current_fence == current.current_fence
                and entry[0].content_digest == current.content_digest
            )

    async def put(
        self, principal: Principal, source: SemanticSource, vector: tuple[float, ...]
    ) -> bool:
        values = checked_vector(vector, source.space)
        async with self._index.transaction.owner((principal.tenant_id, principal.principal_id)):
            current = await self._recapture(principal, source)
            if (
                current is None
                or current.source != source.source
                or current.current_fence != source.current_fence
                or current.content_digest != source.content_digest
                or content_signature(current.record) != content_signature(source.record)
            ):
                return False
            key = (principal.tenant_id, principal.principal_id, current.record.id)
            self._index.semantic_vectors[key] = {
                **self._index.semantic_vectors.get(key, {}),
                self._slot(source): (current.model_copy(deep=True), values),
            }
            return True

    async def search(
        self, query: RecallQuery, space: EmbeddingSpace, vector: tuple[float, ...], *, limit: int
    ) -> list[SemanticHit]:
        values = checked_vector(vector, space)
        check_page(0, 0, limit)
        async with self._index.transaction.owner((query.tenant_id, query.principal_id)):
            # Current originals are filtered before similarity ranking or its limit.
            hits = []
            eligible = await self._memories._eligible_records(query, lexical=False)
            for record in eligible:
                version = self._index.versions[record.id]
                historical = query.known_at is not None or query.as_of is not None
                if query.known_at is not None:
                    selected = self._index.version_at(record.id, query.known_at)
                    if selected is None:
                        continue
                    version = selected
                entry = self._index.semantic_vectors.get(
                    (query.tenant_id, query.principal_id, record.id), {}
                ).get(f"{space.key}:{version.content_revision}" if historical else space.key)
                if entry is None:
                    continue
                source, embedding = entry
                if (
                    source.source != version
                    or (historical and source.current_fence != self._index.versions[record.id])
                    or source.content_digest != text_digest(record)
                ):
                    continue
                similarity = max(
                    -1.0, min(1.0, sum(a * b for a, b in zip(values, embedding, strict=True)))
                )
                hits.append(SemanticHit(record=record, similarity=similarity))
            return sorted(hits, key=lambda h: (-round(h.similarity, 7), str(h.record.id)))[:limit]

    async def bounds(
        self, principal: Principal, *, include_history: bool = False
    ) -> SemanticBounds:
        owner = (principal.tenant_id, principal.principal_id)
        async with self._index.transaction.owner(owner):
            return SemanticBounds(
                creation=len(self._index.changes.get(owner, []))
                if include_history
                else len(self._index.creations.get(owner, [])),
                change=len(self._index.changes.get(owner, [])),
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
        owner = (principal.tenant_id, principal.principal_id)
        async with self._index.transaction.owner(owner):
            bound = min(through, len(self._index.creations.get(owner, [])))
            if after > bound:
                raise ValueError("cursor exceeds owner inventory")
            end = min(bound, after + limit)
            sources = tuple(
                self._index.versions[key]
                for key in self._index.creations.get(owner, [])[after:end]
                if self._index.signatures[key] != "erased"
            )
            return SemanticPage(sources=sources, cursor=end)

    async def changes(
        self, principal: Principal, *, after: int, through: int, limit: int = 64
    ) -> SemanticPage:
        check_page(after, through, limit)
        owner = (principal.tenant_id, principal.principal_id)
        async with self._index.transaction.owner(owner):
            bound = min(through, len(self._index.changes.get(owner, [])))
            if after > bound:
                raise ValueError("cursor exceeds owner journal")
            end = min(bound, after + limit)
            return SemanticPage(
                changes=tuple(self._index.changes.get(owner, [])[after:end]), cursor=end
            )
