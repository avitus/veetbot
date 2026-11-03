"""Vectors for the chunks the knowledge semantic arm has not seen.

knowledge-documents.md ("Indexing") embeds chunks at ingestion, so a document
ingested before the arm was turned on, or under another embedding space, has
no vectors and the dense channel reaches it only through the lexical arm.
This maintenance sweep closes that gap a batch at a time: it embeds the
waiting chunks, in windows, before any unit of work opens, as ingestion
does, then writes the vectors inside one. No document version is made; the
chunks stay the authority and the vectors follow them.
"""

from __future__ import annotations

import logging

from agent_core.domain.agents import Principal
from agent_core.domain.embeddings import EmbeddingError
from agent_core.domain.memory import Sensitivity
from agent_core.knowledge.embedding import KnowledgeEmbedder
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory

logger = logging.getLogger(__name__)


class KnowledgeVectorBackfill:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        embedder: KnowledgeEmbedder,
        principal: Principal,
        clock: Clock,
        batch_chunks: int = 64,
    ) -> None:
        self._uow_factory = uow_factory
        self._embedder = embedder
        self._principal = principal
        self._clock = clock
        self._batch_chunks = batch_chunks

    async def sweep_once(self) -> int:
        """Embed one batch of waiting chunks; return how many chunks gained vectors.

        A withdrawn admission embeds nothing. A document the encoder may not
        see is never listed. A provider failure ends the round with the error
        class logged and nothing written; the local provider admits one
        request at a time, so a round that meets a question being embedded
        simply waits for the next round.
        """

        if not self._embedder.admitted():
            return 0
        allowed = [item for item in Sensitivity if self._embedder.allows(item)]
        async with self._uow_factory() as uow:
            chunks = await uow.knowledge.chunks_without_vectors(
                self._principal.tenant_id,
                self._principal.principal_id,
                self._embedder.space.key,
                sensitivities=allowed,
                limit=self._batch_chunks,
            )
        if not chunks:
            return 0
        try:
            embeddings = await self._embedder.embed_chunks(chunks)
        except EmbeddingError as error:
            # The message is content-free by contract; only the class is logged.
            logger.warning(
                "knowledge_vector_backfill_ended", extra={"error_class": type(error).__name__}
            )
            return 0
        async with self._uow_factory() as uow:
            await uow.knowledge.add_embeddings(embeddings)
        logger.info(
            "knowledge_vectors_backfilled",
            extra={"chunks": len(chunks), "windows": len(embeddings)},
        )
        return len(chunks)
