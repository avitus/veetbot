"""Explicit, content-addressed configuration for offline CPU embeddings."""

import hashlib
import json

from pydantic import Field

from agent_core.domain.semantic_memory import EmbeddingSpace
from agent_core.domain.semantic_work import EmbeddingConfig, SemanticValue

LOCAL_EMBEDDING_REVISION = "local-embedding@1"


class LocalEmbeddingConfig(SemanticValue):
    model: str = Field(min_length=1, max_length=256)
    dimensions: int = Field(ge=1, le=3072)
    model_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tokenizer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    maximum_tokens: int = Field(ge=8, le=512)
    threads: int = Field(ge=1, le=2)

    @property
    def space(self) -> EmbeddingSpace:
        revision = hashlib.sha256(
            json.dumps(
                [
                    LOCAL_EMBEDDING_REVISION,
                    self.model_sha256,
                    self.tokenizer_sha256,
                    self.dimensions,
                    self.maximum_tokens,
                    "masked-mean-l2",
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return EmbeddingSpace(
            provider="onnx",
            model=self.model,
            revision=revision,
            dimensions=self.dimensions,
        )


def local_execution(config: EmbeddingConfig, local: LocalEmbeddingConfig | None) -> bool:
    """Validate the composition's explicit offline tuple, never a caller input."""
    if local is None:
        return False
    checked = LocalEmbeddingConfig.model_validate(local.model_dump())
    if (
        config.space != checked.space
        or config.residency_provider != "local"
        or config.egress_revision != LOCAL_EMBEDDING_REVISION
        or config.usd_per_million_tokens != 0
    ):
        raise ValueError("Invalid local embedding configuration")
    return True
