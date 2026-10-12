"""One explicitly admitted embedding request, without retries or provider routing."""

from typing import Protocol

from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse


class EmbeddingProvider(Protocol):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...
