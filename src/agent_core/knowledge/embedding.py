"""Chunk and question embeddings for the knowledge semantic arm.

knowledge-documents.md ("Indexing") admits a `pgvector` arm behind the fusion
belief recall uses. This module is the only place knowledge text meets an
embedding provider: chunks at ingestion, the question at search. Every call
runs before a unit of work opens, as belief indexing requires, and a failure
reaches the caller as `EmbeddingError` with no provider payload attached.
"""

from __future__ import annotations

from agent_core.domain.embeddings import EmbeddingRequest
from agent_core.domain.knowledge import (
    KNOWLEDGE_EMBEDDING_TEXT_BYTES,
    KnowledgeChunk,
    KnowledgeChunkEmbedding,
    KnowledgeSemanticChannel,
    KnowledgeSemanticQuery,
    knowledge_embedding_text,
)
from agent_core.domain.semantic_memory import EmbeddingSpace
from agent_core.ports.embeddings import EmbeddingProvider

# `EmbeddingRequest` admits at most this many texts per call.
EMBEDDING_BATCH = 32


class KnowledgeEmbedder:
    def __init__(
        self,
        provider: EmbeddingProvider,
        space: EmbeddingSpace,
        *,
        channel: KnowledgeSemanticChannel = "dense",
    ) -> None:
        self._provider = provider
        self._space = space
        self._channel = channel

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    @property
    def channel(self) -> KnowledgeSemanticChannel:
        return self._channel

    async def embed_texts(self, texts: list[str]) -> list[tuple[float, ...]]:
        """Embed texts in request-sized batches, preserving order."""

        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), EMBEDDING_BATCH):
            batch = tuple(texts[start : start + EMBEDDING_BATCH])
            request = EmbeddingRequest(space=self._space, texts=batch)
            response = await self._provider.embed(request)
            vectors.extend(response.validate_for(request))
        return vectors

    async def embed_chunks(
        self, chunks: list[KnowledgeChunk]
    ) -> tuple[KnowledgeChunkEmbedding, ...]:
        vectors = await self.embed_texts(
            [knowledge_embedding_text(chunk.heading_path, chunk.text) for chunk in chunks]
        )
        return tuple(
            KnowledgeChunkEmbedding(chunk_id=chunk.chunk_id, space=self._space, vector=vector)
            for chunk, vector in zip(chunks, vectors, strict=True)
        )

    async def embed_query(self, text: str) -> KnowledgeSemanticQuery:
        question = text.strip().encode("utf-8")[:KNOWLEDGE_EMBEDDING_TEXT_BYTES]
        decoded = question.decode("utf-8", errors="ignore").strip()
        (vector,) = await self.embed_texts([decoded])
        return KnowledgeSemanticQuery(space=self._space, vector=vector, channel=self._channel)
