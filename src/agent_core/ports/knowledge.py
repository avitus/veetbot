"""Knowledge extraction, deterministic chunking, and storage ports."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.knowledge import (
    KnowledgeCatalog,
    KnowledgeCatalogQuery,
    KnowledgeChunk,
    KnowledgeChunkEmbedding,
    KnowledgeDocument,
    KnowledgeIngestPrepared,
    KnowledgeQuery,
    KnowledgeSemanticQuery,
    RetrievedPassage,
)
from agent_core.domain.memory import Sensitivity
from agent_core.domain.trajectory import ArtifactRef


class Extractor(Protocol):
    def media_types(self) -> set[str]: ...

    async def extract(self, source: AsyncIterator[bytes], media_type: str) -> str: ...


class Chunker(Protocol):
    version: str

    def chunk(
        self, text: str, title: str, *, document_row_id: UUID, document_id: UUID, version: int
    ) -> list[KnowledgeChunk]: ...


class KnowledgeStore(Protocol):
    async def ingest(self, prepared: KnowledgeIngestPrepared) -> None: ...

    async def search(
        self, query: KnowledgeQuery, *, semantic: KnowledgeSemanticQuery | None = None
    ) -> list[RetrievedPassage]: ...

    async def latest(self, tenant_id: str, document_id: UUID) -> KnowledgeDocument | None: ...

    async def visible_titles(self, query: KnowledgeQuery) -> list[str]: ...

    async def catalog(self, query: KnowledgeCatalogQuery) -> KnowledgeCatalog:
        """The reader's current documents in title order, capped, with the rest counted.

        The same visibility, tenancy, validity and sensitivity predicate as a
        search; one line per document, its current version only (ADR-0299).
        """
        ...

    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None: ...

    async def chunks_without_vectors(
        self,
        tenant_id: str,
        principal_id: str,
        space_key: str,
        *,
        sensitivities: Sequence[Sensitivity],
        limit: int,
    ) -> list[KnowledgeChunk]:
        """Chunks of the principal's current document versions the space has not seen.

        Only documents of a listed sensitivity, the ones the encoder may see,
        are returned, in document then ordinal order, so the backfill walks
        them the same way on every store.
        """
        ...

    async def remove_embeddings_outside(
        self, tenant_id: str, principal_id: str, space_key: str
    ) -> int:
        """Drop the principal's chunk vectors in any space but this one; return how many.

        Vectors are derived data: a new encoder or window budget is a new
        space, and the backfill fills it while the old space's rows go.
        """
        ...

    async def add_embeddings(self, embeddings: Sequence[KnowledgeChunkEmbedding]) -> None:
        """Store vectors for chunks already ingested; a vector already present is kept."""
        ...

    async def delete(self, document_id: UUID, principal: Principal) -> list[ArtifactRef]: ...
