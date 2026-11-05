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
from agent_core.domain.memory import Sensitivity
from agent_core.domain.semantic_memory import EmbeddingSpace
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT, principal
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
    # Against the question's vector (1, 0, 0): Fans at similarity 1, Rollback
    # at 0.6, Notes at 0, below the semantic floor.
    vectors = {"Rollback": (0.6, 0.8, 0.0), "Fans": (1.0, 0.0, 0.0), "Notes": (0.0, 0.0, 1.0)}
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
    # Below the semantic floor, a chunk is the lexical arm's alone.
    assert by_heading["Notes"].arms == ["lexical"]
    # Rank fusion lifts the passage both arms returned above one only lexical
    # coverage returned, while the semantic-only hit still enters the result.
    assert _headings(passages)[0] in {"Notes", "Rollback"}
    assert "Fans" in _headings(passages)


async def test_dense_channel_orders_by_similarity_alone() -> None:
    store = await _store(_prepared())
    passages = await store.search(_query(text="rollback procedure"), semantic=_semantic("dense"))
    # Notes holds the query's words but sits below the semantic floor, and
    # the space has seen it, so the dense channel leaves it out.
    assert _headings(passages) == ["Fans", "Rollback"]
    assert all(item.arms == ["semantic"] for item in passages)
    # Three quarters similarity, the supplied document's authority, and the
    # scope affinity of a document with no project scope queried from none.
    assert passages[0].score == pytest.approx(0.75 * 1.0 + 0.15 * 0.7 + 0.1 * 1.0)


async def test_a_chunk_without_a_vector_in_the_space_stays_reachable_lexically() -> None:
    store = await _store(_prepared(with_vectors=False))
    hybrid = await store.search(_query(text="rollback procedure"), semantic=_semantic("hybrid"))
    assert _headings(hybrid) == ["Notes", "Rollback"]
    assert all(item.arms == ["lexical"] for item in hybrid)
    # The dense channel, too: a document the space has not seen is ranked by
    # the lexical arm rather than hidden (ADR-0289).
    other_space = _semantic("dense").model_copy(update={"space": OTHER_SPACE})
    dense = await store.search(_query(text="rollback procedure"), semantic=other_space)
    assert _headings(dense) == ["Notes", "Rollback"]
    assert all(item.arms == ["lexical"] for item in dense)


async def test_a_semantic_hit_below_the_floor_is_no_candidate() -> None:
    store = await _store(_prepared())
    # Nothing lexical matches "lunch menu" but Notes; semantically every chunk
    # sits far from this question, so the dense channel answers with nothing
    # the space has seen.
    far = KnowledgeSemanticQuery(space=SPACE, vector=(0.0, 0.0, 1.0), channel="dense")
    passages = await store.search(_query(text="lunch menu"), semantic=far)
    assert _headings(passages) == ["Notes"]
    assert passages[0].arms == ["semantic"]
    unrelated = KnowledgeSemanticQuery(space=SPACE, vector=(0.2, 0.0, 0.98), channel="dense")
    assert passages[0].score > 0
    nothing = await store.search(_query(text="zzz"), semantic=unrelated)
    assert [item.heading_path[-1] for item in nothing] == ["Notes"]


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


def _windowed(number: int = 1) -> KnowledgeIngestPrepared:
    """The fan chunk in two windows: the first far from the question, the second by it."""

    prepared = _document(number, TEXT)
    embeddings: list[KnowledgeChunkEmbedding] = []
    for chunk in prepared.chunks:
        if chunk.heading_path[-1] == "Fans":
            embeddings.append(
                KnowledgeChunkEmbedding(
                    chunk_id=chunk.chunk_id, space=SPACE, window=0, vector=(0.0, 0.0, 1.0)
                )
            )
            embeddings.append(
                KnowledgeChunkEmbedding(
                    chunk_id=chunk.chunk_id, space=SPACE, window=1, vector=(1.0, 0.0, 0.0)
                )
            )
        else:
            embeddings.append(
                KnowledgeChunkEmbedding(
                    chunk_id=chunk.chunk_id, space=SPACE, vector=(0.0, 1.0, 0.0)
                )
            )
    return KnowledgeIngestPrepared(
        document=prepared.document, chunks=prepared.chunks, embeddings=tuple(embeddings)
    )


async def test_a_chunk_is_scored_by_its_best_window() -> None:
    store = await _store(_windowed())
    passages = await store.search(_query(text="lunch menu"), semantic=_semantic("dense"))
    assert _headings(passages) == ["Fans"]
    assert passages[0].arms == ["semantic"]
    assert passages[0].score == pytest.approx(0.75 * 1.0 + 0.15 * 0.7 + 0.1 * 1.0)


async def test_chunks_without_a_vector_in_the_space_are_listed_for_backfill() -> None:
    store = await _store(_prepared(1, with_vectors=False), _prepared(2))
    waiting = await store.chunks_without_vectors(
        TENANT, PRINCIPAL_ID, SPACE.key, sensitivities=(Sensitivity.INTERNAL,), limit=10
    )
    assert {chunk.document_id for chunk in waiting} == {UUID(int=2_001)}
    assert [chunk.ordinal for chunk in waiting] == [0, 1, 2]
    # Another principal's documents, a sensitivity the encoder may not see,
    # and a superseded version are not backfilled.
    assert (
        await store.chunks_without_vectors(
            TENANT, "principal-b", SPACE.key, sensitivities=(Sensitivity.INTERNAL,), limit=10
        )
        == []
    )
    assert (
        await store.chunks_without_vectors(
            TENANT, PRINCIPAL_ID, SPACE.key, sensitivities=(Sensitivity.PUBLIC,), limit=10
        )
        == []
    )
    limited = await store.chunks_without_vectors(
        TENANT, PRINCIPAL_ID, OTHER_SPACE.key, sensitivities=(Sensitivity.INTERNAL,), limit=2
    )
    assert len(limited) == 2
    newer = _document(1, "# Guide\n\n## Rollback\n\nA newer rollback.", valid_from=NOW)
    newer = KnowledgeIngestPrepared(
        document=newer.document.model_copy(update={"row_id": UUID(int=1_900), "version": 2}),
        chunks=[
            chunk.model_copy(update={"document_row_id": UUID(int=1_900), "version": 2})
            for chunk in newer.chunks
        ],
    )
    await store.ingest(newer)
    waiting = await store.chunks_without_vectors(
        TENANT, PRINCIPAL_ID, SPACE.key, sensitivities=(Sensitivity.INTERNAL,), limit=10
    )
    assert [chunk.document_row_id for chunk in waiting] == [UUID(int=1_900)]


async def test_added_vectors_are_searched_and_repeating_them_is_harmless() -> None:
    store = await _store(_prepared(with_vectors=False))
    chunks = await store.chunks_without_vectors(
        TENANT, PRINCIPAL_ID, SPACE.key, sensitivities=(Sensitivity.INTERNAL,), limit=10
    )
    fans = next(chunk for chunk in chunks if chunk.heading_path[-1] == "Fans")
    added = (
        KnowledgeChunkEmbedding(chunk_id=fans.chunk_id, space=SPACE, window=0, vector=(0, 0, 1.0)),
        KnowledgeChunkEmbedding(chunk_id=fans.chunk_id, space=SPACE, window=1, vector=(1.0, 0, 0)),
    )
    await store.add_embeddings(added)
    await store.add_embeddings(added)
    passages = await store.search(_query(text="lunch menu"), semantic=_semantic("dense"))
    assert _headings(passages) == ["Fans", "Notes"]
    assert passages[0].arms == ["semantic"] and passages[1].arms == ["lexical"]
    left = await store.chunks_without_vectors(
        TENANT, PRINCIPAL_ID, SPACE.key, sensitivities=(Sensitivity.INTERNAL,), limit=10
    )
    assert fans.chunk_id not in {chunk.chunk_id for chunk in left} and len(left) == 2


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
    # Another window of the same chunk in the same space is a second vector, not a repeat.
    second = twice.model_copy(update={"window": 1})
    assert (
        len(
            KnowledgeIngestPrepared(
                document=prepared.document, chunks=prepared.chunks, embeddings=(twice, second)
            ).embeddings
        )
        == 2
    )


async def test_semantic_reservation_reaches_past_a_verbose_document() -> None:
    """A later document's first passage is reserved however many chunks rank above it (ADR-0293).

    Neither arm is cut to a candidate limit before the per-document
    reservation: a document of seventy chunks nearer the question than
    another document's only passage does not hide that passage.
    """

    filler = " ".join(f"word{index}" for index in range(120))
    verbose = _document(
        1,
        "\n\n".join(
            f"# Step {index}\n\nStep {index} of the procedure. {filler}" for index in range(70)
        ),
    )
    verbose = verbose.model_copy(
        update={
            "embeddings": tuple(
                KnowledgeChunkEmbedding(
                    chunk_id=chunk.chunk_id, space=SPACE, vector=(1.0, 0.0, 0.0)
                )
                for chunk in verbose.chunks
            )
        }
    )
    other = _document(2, "# Other\n\nThe other way, once.")
    other = other.model_copy(
        update={
            "embeddings": tuple(
                KnowledgeChunkEmbedding(
                    chunk_id=chunk.chunk_id, space=SPACE, vector=(0.9, 0.436, 0.0)
                )
                for chunk in other.chunks
            )
        }
    )
    assert len(verbose.chunks) == 70
    store = await _store(verbose, other)
    passages = await store.search(
        _query(text="procedure", max_passages=8, max_per_document=3), semantic=_semantic("dense")
    )
    assert "Other" in _headings(passages)[:4]
    assert len(passages) == 8
