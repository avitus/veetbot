"""Chunk and question embeddings for the knowledge semantic arm.

knowledge-documents.md ("Indexing") admits a `pgvector` arm behind the fusion
belief recall uses. This module is the only place knowledge text meets an
embedding provider: chunks at ingestion, the question at search. Every call
runs before a unit of work opens, as belief indexing requires, and a failure
reaches the caller as `EmbeddingError` with no provider payload attached.
"""

from __future__ import annotations

from collections.abc import Callable

from agent_core.domain.embeddings import EmbeddingRequest
from agent_core.domain.knowledge import (
    KNOWLEDGE_EMBEDDING_TEXT_BYTES,
    KnowledgeChunk,
    KnowledgeChunkEmbedding,
    KnowledgeSemanticChannel,
    KnowledgeSemanticQuery,
    knowledge_embedding_windows,
)
from agent_core.domain.memory import PROVIDER_EGRESS_SENSITIVITIES, Sensitivity
from agent_core.domain.semantic_memory import EmbeddingSpace
from agent_core.ports.embeddings import EmbeddingProvider

# `EmbeddingRequest` admits at most this many texts per call.
EMBEDDING_BATCH = 32


class KnowledgeAdmissionWithdrawnError(Exception):
    """The semantic index's admission was withdrawn between provider requests.

    Distinct from `EmbeddingError`, the provider's own failure: ingestion
    answers it by leaving the document lexical, search by the lexical arm,
    and the backfill by writing nothing for the interrupted group.
    """


class KnowledgeEmbedder:
    """The knowledge arm's one route to an embedding provider.

    `admitted` is the semantic index's own admission check, read before every
    provider call, so a withdrawn admission stops knowledge egress with the
    rest. `local` says the provider runs on this host: a remote provider
    receives only public and internal text, the sensitivities the memory
    index may send out (`PROVIDER_EGRESS_SENSITIVITIES`), and a document
    above that stays lexical. `window_tokens` is the encoder's input
    capacity, with `count_tokens` its own tokenizer's count: a chunk over
    the capacity is embedded as windows that each fit it, one vector a
    window (`knowledge_embedding_windows`); without a capacity a chunk is
    one window.
    """

    def __init__(
        self,
        provider: EmbeddingProvider,
        space: EmbeddingSpace,
        *,
        channel: KnowledgeSemanticChannel = "dense",
        local: bool = False,
        admitted: Callable[[], bool] = lambda: True,
        window_tokens: int | None = None,
        count_tokens: Callable[[str], int] | None = None,
    ) -> None:
        self._provider = provider
        self._space = space
        self._channel = channel
        self._local = local
        self._admitted = admitted
        self._window_tokens = window_tokens
        self._count_tokens = count_tokens

    def admitted(self) -> bool:
        return bool(self._admitted())

    def allows(self, sensitivity: Sensitivity) -> bool:
        """Whether text of this sensitivity may reach the provider."""

        return self._local or sensitivity in PROVIDER_EGRESS_SENSITIVITIES

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
            # Admission is read before every request, not once a call: a
            # withdrawal between batches stops the next one, and the vectors
            # already made are not returned.
            if not self.admitted():
                raise KnowledgeAdmissionWithdrawnError
            batch = tuple(texts[start : start + EMBEDDING_BATCH])
            request = EmbeddingRequest(space=self._space, texts=batch)
            response = await self._provider.embed(request)
            vectors.extend(response.validate_for(request))
        return vectors

    def window_texts(self, heading_path: list[str], text: str) -> list[str]:
        """The texts a chunk with this heading path and text is embedded as."""

        return knowledge_embedding_windows(
            heading_path,
            text,
            budget_tokens=self._window_tokens,
            count_tokens=self._count_tokens,
        )

    async def embed_chunks(
        self, chunks: list[KnowledgeChunk]
    ) -> tuple[KnowledgeChunkEmbedding, ...]:
        """One vector per window of every chunk, in chunk then window order."""

        windows = [self.window_texts(chunk.heading_path, chunk.text) for chunk in chunks]
        vectors = iter(await self.embed_texts([text for texts in windows for text in texts]))
        return tuple(
            KnowledgeChunkEmbedding(
                chunk_id=chunk.chunk_id, space=self._space, window=index, vector=next(vectors)
            )
            for chunk, texts in zip(chunks, windows, strict=True)
            for index in range(len(texts))
        )

    async def embed_query(self, text: str) -> KnowledgeSemanticQuery:
        question = text.strip().encode("utf-8")[:KNOWLEDGE_EMBEDDING_TEXT_BYTES]
        decoded = question.decode("utf-8", errors="ignore").strip()
        (vector,) = await self.embed_texts([decoded])
        return KnowledgeSemanticQuery(space=self._space, vector=vector, channel=self._channel)
