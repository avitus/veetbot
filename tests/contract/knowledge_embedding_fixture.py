"""A replayed embedding provider for the knowledge paraphrase corpus gate.

`evals/fixtures/embeddings/knowledge_paraphrase.json` holds vectors a real
embedding model produced once for every chunk and question in the paraphrase
corpus (`scripts/record_knowledge_embeddings.py` records it). The gate replays
them by text digest, so it measures a real model's geometry without a network
call or a provider SDK in `make check`, and a text the fixture has not seen is
an error rather than a guess.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest, EmbeddingResponse
from agent_core.domain.knowledge import KnowledgeSemanticChannel
from agent_core.domain.semantic_memory import EmbeddingSpace
from agent_core.knowledge.embedding import KnowledgeEmbedder

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "evals/fixtures/embeddings/knowledge_paraphrase.json"
LOCAL_FIXTURE = ROOT / "evals/fixtures/embeddings/knowledge_paraphrase.onnx.json"


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RecordedEmbeddingProvider:
    def __init__(self, path: Path = FIXTURE) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        self.space = EmbeddingSpace.model_validate(document["space"])
        self._vectors: dict[str, tuple[float, ...]] = {
            key: tuple(float(value) for value in vector)
            for key, vector in document["vectors"].items()
        }
        # A local recording carries the encoder's capacity and every token
        # count the windowing asked for, so replay cuts the same windows
        # without the tokenizer (ADR-0290).
        self.window_tokens: int | None = document.get("window_tokens")
        self.whole_tokens: int | None = document.get("whole_tokens")
        self._counts: dict[str, int] = {
            key: int(value) for key, value in document.get("token_counts", {}).items()
        }
        self.calls: list[EmbeddingRequest] = []

    def count_tokens(self, text: str) -> int:
        count = self._counts.get(text_key(text))
        if count is None:
            raise EmbeddingError("text is not in the recorded token counts")
        return count

    def embedder(self, *, channel: KnowledgeSemanticChannel = "dense") -> KnowledgeEmbedder:
        """An embedder that cuts the windows the recording was made with."""

        if self.window_tokens is None:
            return KnowledgeEmbedder(self, self.space, channel=channel)
        # A local recording replays its tokenizer's counts; an OpenAI recording
        # was windowed by the chunker's estimate, which needs no counts.
        return KnowledgeEmbedder(
            self,
            self.space,
            channel=channel,
            window_tokens=self.window_tokens,
            count_tokens=self.count_tokens if self._counts else None,
            whole_tokens=self.whole_tokens,
        )

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.calls.append(request)
        if request.space != self.space:
            raise EmbeddingError("recorded embedding space mismatch")
        vectors: list[tuple[float, ...]] = []
        for text in request.texts:
            vector = self._vectors.get(text_key(text))
            if vector is None:
                raise EmbeddingError("text is not in the recorded embedding fixture")
            vectors.append(vector)
        return EmbeddingResponse(
            request_digest=request.digest,
            vectors=tuple(vectors),
            input_tokens=max(1, sum(len(text.encode("utf-8")) // 4 for text in request.texts)),
        )
