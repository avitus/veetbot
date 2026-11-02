#!/usr/bin/env python3
"""Record real embeddings for the knowledge paraphrase corpus gate (ADR-0289).

The gate `gate.knowledge.paraphrase_lift` replays vectors from
``evals/fixtures/embeddings/knowledge_paraphrase.json`` so that `make check`
measures a real embedding model's geometry without a network call. This
script is the one place those vectors come from. It embeds every chunk the
deterministic chunker produces for every corpus document, exactly as
ingestion embeds them, plus every question, through the project's own
OpenAI adapter, and writes the fixture.

Run it after changing a corpus member, the chunker, the embedding text
version, or the space:

    doppler run -- uv run python scripts/record_knowledge_embeddings.py

The key is read from ``VEETBOT_OPENAI_KEY`` and never written anywhere.

``--report`` needs no key: it replays the fixture through the in-memory
store and prints, for every corpus member, where the answering passage ranks
under lexical search alone, under the hybrid channel, under the dense
channel, and in the semantic arm by itself, with the `knowledge.search`
tool's own parameters. The ADR-0289 evidence table is this report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent_core.adapters.embeddings.openai import OpenAIEmbeddingProvider  # noqa: E402
from agent_core.domain.knowledge import knowledge_embedding_text  # noqa: E402
from agent_core.domain.semantic_memory import EmbeddingSpace  # noqa: E402
from agent_core.knowledge.chunking import DeterministicChunker, normalize_text  # noqa: E402
from agent_core.knowledge.embedding import KnowledgeEmbedder  # noqa: E402

CORPUS = ROOT / "evals/corpora/knowledge_paraphrase"
FIXTURE = ROOT / "evals/fixtures/embeddings/knowledge_paraphrase.json"
# text-embedding-3-small at its native width; the revision names the fixture's
# recording so a re-record is a visible change.
SPACE = EmbeddingSpace(
    provider="openai",
    model="text-embedding-3-small",
    revision="knowledge-paraphrase-fixture@1",
    dimensions=1536,
)


def corpus_texts() -> list[str]:
    chunker = DeterministicChunker()
    texts: list[str] = []
    for path in sorted(CORPUS.glob("*.json")):
        member = json.loads(path.read_text(encoding="utf-8"))
        chunks = chunker.chunk(
            normalize_text(member["document"]),
            member["title"],
            document_row_id=UUID(int=0),
            document_id=UUID(int=0),
            version=1,
        )
        texts.extend(knowledge_embedding_text(chunk.heading_path, chunk.text) for chunk in chunks)
        texts.append(member["question"].strip())
    return list(dict.fromkeys(texts))


async def report() -> int:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
    from agent_core.domain.knowledge import KnowledgeIngestPrepared, KnowledgeQuery
    from tests.contract.knowledge_embedding_fixture import RecordedEmbeddingProvider
    from tests.contract.support import NOW, PRINCIPAL_ID, TENANT
    from tests.contract.test_knowledge_passage_search_contract import _document

    provider = RecordedEmbeddingProvider(FIXTURE)
    store = InMemoryKnowledgeStore(FixedClock(NOW))
    members: list[tuple[dict[str, str], KnowledgeIngestPrepared]] = []
    for number, path in enumerate(sorted(CORPUS.glob("*.json")), 1):
        member = json.loads(path.read_text(encoding="utf-8"))
        prepared = _document(number, normalize_text(member["document"]))
        embeddings = await KnowledgeEmbedder(provider, provider.space).embed_chunks(prepared.chunks)
        prepared = KnowledgeIngestPrepared(
            document=prepared.document, chunks=prepared.chunks, embeddings=embeddings
        )
        await store.ingest(prepared)
        members.append((member, prepared))
    print(f"{'member':<20} {'lexical':>8} {'hybrid':>8} {'dense':>8} {'semantic arm':>13}")
    for member, prepared in members:
        query = KnowledgeQuery(
            tenant_id=TENANT,
            principal_id=PRINCIPAL_ID,
            current_scope=None,
            text=member["question"],
            budget_tokens=3_000,
            max_passages=10,
            max_per_document=2,
            min_score=0.1,
        )
        ranks: list[str] = []
        for channel in (None, "hybrid", "dense"):
            semantic = None
            if channel is not None:
                semantic = await KnowledgeEmbedder(
                    provider, provider.space, channel=channel
                ).embed_query(member["question"])
            passages = await store.search(query, semantic=semantic)
            rank = next(
                (i for i, item in enumerate(passages, 1) if member["answer"] in item.text), None
            )
            ranks.append("miss" if rank is None else str(rank))
        question = await KnowledgeEmbedder(provider, provider.space).embed_query(member["question"])
        by_vector = {e.chunk_id: e.vector for e in prepared.embeddings}
        arm = sorted(
            prepared.chunks,
            key=lambda chunk: (
                -sum(a * b for a, b in zip(question.vector, by_vector[chunk.chunk_id], strict=True))
            ),
        )
        within = next(i for i, chunk in enumerate(arm, 1) if member["answer"] in chunk.text)
        print(
            f"{member['title'][:20]:<20} {ranks[0]:>8} {ranks[1]:>8} {ranks[2]:>8} "
            f"{within:>4} of {len(arm):<6}"
        )
    return 0


async def main() -> int:
    if "--report" in sys.argv[1:]:
        return await report()
    key = os.environ.get("VEETBOT_OPENAI_KEY", "").strip()
    if not key:
        print("VEETBOT_OPENAI_KEY is not set", file=sys.stderr)
        return 2
    texts = corpus_texts()
    embedder = KnowledgeEmbedder(OpenAIEmbeddingProvider(key), SPACE)
    vectors = await embedder.embed_texts(texts)
    fixture = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "space": SPACE.model_dump(mode="json"),
        "texts": len(texts),
        "vectors": {
            hashlib.sha256(text.encode("utf-8")).hexdigest(): [round(value, 6) for value in vector]
            for text, vector in zip(texts, vectors, strict=True)
        },
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixture, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"recorded {len(texts)} texts into {FIXTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
