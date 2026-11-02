"""The knowledge semantic arm: vectors at ingest, two channels at search, one cascade.

knowledge-documents.md ("Indexing") builds `pgvector` behind the fusion memory
uses. Both stores answer the same way: without a semantic query the lexical
ranking of ADR-0285 is unchanged; with one, the `hybrid` channel fuses both
arms and the `dense` channel orders by similarity alone; a chunk without a
vector in the query's space is lexical only; deleting the document removes its
vectors with its chunks.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
from agent_core.domain.knowledge import (
    KnowledgeChunkEmbedding,
    KnowledgeIngestPrepared,
    KnowledgeSemanticQuery,
    RetrievedPassage,
)
from agent_core.domain.semantic_memory import EmbeddingSpace
from tests.contract.support import NOW, principal
from tests.contract.test_knowledge_passage_search_contract import _document, _query

SPACE = EmbeddingSpace(provider="fixture", model="semantic", revision="v1", dimensions=3)
OTHER_SPACE = SPACE.model_copy(update={"revision": "v2"})
TEXT = (
    "# Guide\n\n## Rollback\n\nRoll back the release by reverting the tag.\n\n"
    "## Fans\n\nThe cooling fan may run for several minutes after shutdown.\n\n"
    "## Notes\n\nNothing about the rollback procedure here, only the procedure for lunch."
)


def _prepared(number: int = 1, *, with_vectors: bool = True) -> KnowledgeIngestPrepared:
    prepared = _document(number, TEXT)
    if not with_vectors:
        return prepared
    vectors = {"Rollback": (0.0, 1.0, 0.0), "Fans": (1.0, 0.0, 0.0), "Notes": (0.0, 0.0, 1.0)}
    embeddings = tuple(
        KnowledgeChunkEmbedding(
            chunk_id=chunk.chunk_id, space=SPACE, vector=vectors[chunk.heading_path[-1]]
        )
        for chunk in prepared.chunks
    )
    return KnowledgeIngestPrepared(
        document=prepared.document, chunks=prepared.chunks, embeddings=embeddings
    )


def _semantic(channel: str = "hybrid") -> KnowledgeSemanticQuery:
    # The question "sounds after parking" embeds next to the fan passage.
    return KnowledgeSemanticQuery(space=SPACE, vector=(1.0, 0.0, 0.0), channel=channel)  # type: ignore[arg-type]


async def _store(*prepared: KnowledgeIngestPrepared) -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore(FixedClock(NOW))
    for item in prepared:
        await store.ingest(item)
    return store


def _headings(passages: list[RetrievedPassage]) -> list[str]:
    return [item.heading_path[-1] for item in passages]


async def test_without_a_semantic_query_the_lexical_ranking_is_unchanged() -> None:
    store = await _store(_prepared())
    passages = await store.search(_query(text="rollback procedure"))
    assert _headings(passages) == ["Notes", "Rollback"]
    assert all(item.arms == ["lexical"] for item in passages)


async def test_hybrid_channel_fuses_both_arms_and_names_them() -> None:
    store = await _store(_prepared())
    passages = await store.search(
        _query(text="rollback procedure", max_per_document=3), semantic=_semantic("hybrid")
    )
    by_heading = {item.heading_path[-1]: item for item in passages}
    assert by_heading["Fans"].arms == ["semantic"]
    assert by_heading["Rollback"].arms == ["lexical", "semantic"]
    assert by_heading["Notes"].arms == ["lexical", "semantic"]
    # Rank fusion lifts the passage both arms returned above one only lexical
    # coverage returned, while the semantic-only hit still enters the result.
    assert _headings(passages)[0] in {"Notes", "Rollback"}
    assert "Fans" in _headings(passages)


async def test_dense_channel_orders_by_similarity_alone() -> None:
    store = await _store(_prepared())
    passages = await store.search(_query(text="rollback procedure"), semantic=_semantic("dense"))
    assert _headings(passages)[0] == "Fans"
    assert all(item.arms == ["semantic"] for item in passages)
    # Three quarters similarity, the supplied document's authority, and the
    # scope affinity of a document with no project scope queried from none.
    assert passages[0].score == pytest.approx(0.75 * 1.0 + 0.15 * 0.7 + 0.1 * 1.0)


async def test_a_chunk_without_a_vector_in_the_space_is_lexical_only() -> None:
    store = await _store(_prepared(with_vectors=False))
    hybrid = await store.search(_query(text="rollback procedure"), semantic=_semantic("hybrid"))
    assert _headings(hybrid) == ["Notes", "Rollback"]
    assert all(item.arms == ["lexical"] for item in hybrid)
    other_space = _semantic("dense").model_copy(update={"space": OTHER_SPACE})
    assert await store.search(_query(text="rollback procedure"), semantic=other_space) == []


async def test_semantic_hits_respect_visibility_and_the_per_document_cap() -> None:
    store = await _store(_prepared(1), _prepared(2))
    colleague = _query(text="rollback procedure", principal_id="principal-b")
    assert await store.search(colleague, semantic=_semantic("dense")) == []
    owner = await store.search(_query(text="rollback procedure"), semantic=_semantic("dense"))
    per_document: dict[UUID, int] = {}
    for item in owner:
        per_document[item.document_id] = per_document.get(item.document_id, 0) + 1
    assert set(per_document.values()) == {2}


async def test_deleting_the_document_removes_its_vectors() -> None:
    store = await _store(_prepared())
    await store.delete(UUID(int=2_001), principal())
    assert await store.search(_query(text="rollback procedure"), semantic=_semantic("dense")) == []
    assert store._vectors == {}


def test_prepared_ingest_rejects_vectors_for_unknown_chunks() -> None:
    prepared = _prepared(with_vectors=False)
    stray = KnowledgeChunkEmbedding(
        chunk_id="kc_0123456789abcdef", space=SPACE, vector=(1.0, 0.0, 0.0)
    )
    with pytest.raises(ValueError, match="outside the ingest"):
        KnowledgeIngestPrepared(
            document=prepared.document, chunks=prepared.chunks, embeddings=(stray,)
        )
    first = prepared.chunks[0].chunk_id
    twice = KnowledgeChunkEmbedding(chunk_id=first, space=SPACE, vector=(1.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="repeats"):
        KnowledgeIngestPrepared(
            document=prepared.document, chunks=prepared.chunks, embeddings=(twice, twice)
        )
