"""Knowledge extraction, deterministic chunking, and storage ports."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestPrepared,
    KnowledgeQuery,
    RetrievedPassage,
)
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

    async def search(self, query: KnowledgeQuery) -> list[RetrievedPassage]: ...

    async def latest(self, tenant_id: str, document_id: UUID) -> KnowledgeDocument | None: ...

    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None: ...

    async def delete(self, document_id: UUID, principal: Principal) -> list[ArtifactRef]: ...
