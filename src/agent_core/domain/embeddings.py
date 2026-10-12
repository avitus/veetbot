"""Provider-neutral bounded embedding requests; private inputs remain transient."""

import hashlib
from typing import Self

from pydantic import Field, model_validator

from agent_core.domain.semantic_memory import EmbeddingSpace, checked_vector
from agent_core.domain.semantic_work import SemanticValue


class EmbeddingRequest(SemanticValue):
    space: EmbeddingSpace
    texts: tuple[str, ...] = Field(min_length=1, max_length=32, repr=False)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if any(not text.strip() or len(text.encode()) > 4096 for text in self.texts):
            raise ValueError("embedding input exceeds per-item bound")
        if self.maximum_tokens > 65536:
            raise ValueError("embedding batch exceeds token bound")
        return self

    @property
    def maximum_tokens(self) -> int:
        return sum(len(text.encode()) + 1 for text in self.texts)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class EmbeddingResponse(SemanticValue):
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    vectors: tuple[tuple[float, ...], ...] = Field(min_length=1, max_length=32, repr=False)
    input_tokens: int = Field(gt=0, le=65536, strict=True)

    def validate_for(self, request: EmbeddingRequest) -> tuple[tuple[float, ...], ...]:
        if (
            self.request_digest != request.digest
            or len(self.vectors) != len(request.texts)
            or self.input_tokens > request.maximum_tokens
        ):
            raise ValueError("embedding response does not match admission")
        return tuple(checked_vector(v, request.space) for v in self.vectors)


class EmbeddingError(ValueError):
    """Content-free transport/protocol error; no provider response is retained."""
