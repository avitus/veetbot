"""One explicitly admitted embedding request, without retries or provider routing."""

from typing import Protocol

from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse


class EmbeddingProvider(Protocol):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...


class LocalEmbeddingProvider(EmbeddingProvider, Protocol):
    @property
    def maximum_tokens(self) -> int:
        """The input capacity one text may use; a longer input is refused, not cut."""
        ...

    def count_tokens(self, text: str) -> int:
        """How many tokens a text costs by the encoder's own tokenizer."""
        ...

    async def close(self) -> None: ...
