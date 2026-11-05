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

``--local DIR`` records the production local encoder's space instead
(ADR-0188's MiniLM tuple: ``model.onnx`` and ``tokenizer.json`` in DIR, 384
dimensions, a 256-token capacity) into ``knowledge_paraphrase.onnx.json``.
Chunks are cut into the windows ingestion would cut (ADR-0290), and every
token count the windowing asked for is recorded beside the vectors so the
gate replays the same windows without the tokenizer.

``--report [--fixture PATH]`` needs no key or model: it replays a fixture
through the in-memory store and prints, for every corpus member, where the
answering passage ranks under lexical search alone, under the hybrid
channel, under the dense channel, and in the semantic arm by itself, with the
`knowledge.search` tool's own parameters, then the similarities the
`KNOWLEDGE_SEMANTIC_MIN_SIMILARITY` floor must sit between: the answering
chunk's, the best other document's, and the best any chunk reaches for the
unrelated question. The ADR-0289 and ADR-0290 evidence tables are this report.
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
from agent_core.domain.knowledge import (  # noqa: E402
    KNOWLEDGE_OPENAI_WHOLE_TOKENS,
    KNOWLEDGE_OPENAI_WINDOW_TOKENS,
)
from agent_core.domain.local_embeddings import LocalEmbeddingConfig  # noqa: E402
from agent_core.domain.semantic_memory import EmbeddingSpace  # noqa: E402
from agent_core.knowledge.chunking import DeterministicChunker, normalize_text  # noqa: E402
from agent_core.knowledge.embedding import KnowledgeEmbedder  # noqa: E402

CORPUS = ROOT / "evals/corpora/knowledge_paraphrase"
FIXTURE = ROOT / "evals/fixtures/embeddings/knowledge_paraphrase.json"
LOCAL_FIXTURE = ROOT / "evals/fixtures/embeddings/knowledge_paraphrase.onnx.json"
# The production local encoder (ADR-0188): the files are verified by digest
# when loaded, so the digests are read from the directory given.
LOCAL_MODEL = "all-MiniLM-L6-v2"
LOCAL_DIMENSIONS = 384
LOCAL_MAXIMUM_TOKENS = 256
# text-embedding-3-small at its native width; the revision names the fixture's
# recording so a re-record is a visible change.
SPACE = EmbeddingSpace(
    provider="openai",
    model="text-embedding-3-small",
    revision="knowledge-paraphrase-fixture@1",
    dimensions=1536,
)


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def corpus_texts(embedder: KnowledgeEmbedder) -> list[str]:
    """Every window ingestion would embed, plus every question, in corpus order."""

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
        for chunk in chunks:
            texts.extend(embedder.window_texts(chunk.heading_path, chunk.text))
        texts.append(member["question"].strip())
        # A question none of the documents answers, for the negative case.
        texts.append(member["unrelated_question"].strip())
    return list(dict.fromkeys(texts))


def local_config(directory: Path) -> LocalEmbeddingConfig:
    return LocalEmbeddingConfig(
        model=LOCAL_MODEL,
        dimensions=LOCAL_DIMENSIONS,
        model_sha256=hashlib.sha256((directory / "model.onnx").read_bytes()).hexdigest(),
        tokenizer_sha256=hashlib.sha256((directory / "tokenizer.json").read_bytes()).hexdigest(),
        maximum_tokens=LOCAL_MAXIMUM_TOKENS,
        threads=1,
    )


async def report(fixture: Path) -> int:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
    from agent_core.domain.knowledge import KnowledgeIngestPrepared, KnowledgeQuery
    from tests.contract.knowledge_embedding_fixture import RecordedEmbeddingProvider
    from tests.contract.support import NOW, PRINCIPAL_ID, TENANT
    from tests.contract.test_knowledge_passage_search_contract import _document

    provider = RecordedEmbeddingProvider(fixture)
    print(f"fixture {fixture.name}: {provider.space.provider} {provider.space.model}")
    store = InMemoryKnowledgeStore(FixedClock(NOW))
    members: list[tuple[dict[str, str], KnowledgeIngestPrepared]] = []
    for number, path in enumerate(sorted(CORPUS.glob("*.json")), 1):
        member = json.loads(path.read_text(encoding="utf-8"))
        prepared = _document(number, normalize_text(member["document"]))
        embeddings = await provider.embedder().embed_chunks(prepared.chunks)
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
                semantic = await provider.embedder(channel=channel).embed_query(member["question"])
            passages = await store.search(query, semantic=semantic)
            rank = next(
                (i for i, item in enumerate(passages, 1) if member["answer"] in item.text), None
            )
            ranks.append("miss" if rank is None else str(rank))
        question = await provider.embedder().embed_query(member["question"])
        # A chunk's similarity is its best window's, as both stores score it.
        best = {
            chunk.chunk_id: max(
                sum(a * b for a, b in zip(question.vector, e.vector, strict=True))
                for e in prepared.embeddings
                if e.chunk_id == chunk.chunk_id
            )
            for chunk in prepared.chunks
        }
        arm = sorted(prepared.chunks, key=lambda chunk: -best[chunk.chunk_id])
        within = next(i for i, chunk in enumerate(arm, 1) if member["answer"] in chunk.text)
        print(
            f"{member['title'][:20]:<20} {ranks[0]:>8} {ranks[1]:>8} {ranks[2]:>8} "
            f"{within:>4} of {len(arm):<6}"
        )
    # The floor: what the answering chunk scores, what the nearest chunk of any
    # other document scores, and what the nearest chunk anywhere scores for the
    # question none of the documents answers.
    print()
    print(f"{'member':<20} {'answer':>8} {'other doc':>10} {'unrelated':>10}")
    all_vectors = [(m, e) for m, p in members for e in p.embeddings]
    for member, prepared in members:
        embedder = provider.embedder()
        question = await embedder.embed_query(member["question"])
        unrelated = await embedder.embed_query(member["unrelated_question"])
        dot = lambda a, b: sum(x * y for x, y in zip(a, b, strict=True))  # noqa: E731
        answer = max(
            dot(question.vector, e.vector)
            for e in prepared.embeddings
            if member["answer"] in next(c.text for c in prepared.chunks if c.chunk_id == e.chunk_id)
        )
        other = max(dot(question.vector, e.vector) for m, e in all_vectors if m is not member)
        anywhere = max(dot(unrelated.vector, e.vector) for _, e in all_vectors)
        print(f"{member['title'][:20]:<20} {answer:>8.3f} {other:>10.3f} {anywhere:>10.3f}")
    return 0


def _option(name: str) -> str | None:
    arguments = sys.argv[1:]
    if name not in arguments:
        return None
    index = arguments.index(name)
    if index + 1 >= len(arguments):
        raise SystemExit(f"{name} needs a value")
    return arguments[index + 1]


async def main() -> int:
    if "--report" in sys.argv[1:]:
        fixture = _option("--fixture")
        return await report(FIXTURE if fixture is None else Path(fixture).resolve())
    local = _option("--local")
    counts: dict[str, int] = {}
    if local is not None:
        from agent_core.adapters.models.local_embeddings import LocalEmbeddingProvider

        config = local_config(Path(local).resolve())
        provider = LocalEmbeddingProvider(Path(local).resolve(), config)

        def count_tokens(text: str) -> int:
            # Every count the windowing asks for is recorded, so the gate's
            # replay cuts the same windows without the tokenizer.
            count = provider.count_tokens(text)
            counts[text_key(text)] = count
            return count

        space = config.space
        budget = _option("--window-tokens")
        window_tokens = config.maximum_tokens if budget is None else int(budget)
        embedder = KnowledgeEmbedder(
            provider, space, window_tokens=window_tokens, count_tokens=count_tokens
        )
        target = LOCAL_FIXTURE
    else:
        key = os.environ.get("VEETBOT_OPENAI_KEY", "").strip()
        if not key:
            print("VEETBOT_OPENAI_KEY is not set", file=sys.stderr)
            return 2
        space = SPACE
        # The OpenAI route embeds in the measured windows (ADR-0290), by the
        # chunker's estimate, so the fixture holds the windows ingestion makes.
        embedder = KnowledgeEmbedder(
            OpenAIEmbeddingProvider(key),
            space,
            window_tokens=KNOWLEDGE_OPENAI_WINDOW_TOKENS,
            whole_tokens=KNOWLEDGE_OPENAI_WHOLE_TOKENS,
        )
        target = FIXTURE
    texts = corpus_texts(embedder)
    vectors = await embedder.embed_texts(texts)
    fixture: dict[str, object] = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "space": space.model_dump(mode="json"),
        "texts": len(texts),
        "vectors": {
            text_key(text): [round(value, 6) for value in vector]
            for text, vector in zip(texts, vectors, strict=True)
        },
    }
    if local is not None:
        fixture["window_tokens"] = window_tokens
        fixture["token_counts"] = dict(sorted(counts.items()))
        fixture["model_sha256"] = config.model_sha256
        fixture["tokenizer_sha256"] = config.tokenizer_sha256
    else:
        fixture["window_tokens"] = KNOWLEDGE_OPENAI_WINDOW_TOKENS
        fixture["whole_tokens"] = KNOWLEDGE_OPENAI_WHOLE_TOKENS
    out = _option("--out")
    if out is not None:
        target = Path(out).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fixture, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"recorded {len(texts)} texts into {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
