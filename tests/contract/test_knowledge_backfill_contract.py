"""The knowledge vector backfill: documents the arm never saw gain vectors in rounds.

knowledge-documents.md ("Indexing") embeds chunks at ingestion, so a document
ingested before the arm was turned on, or in another space, has no vectors.
The maintenance sweep embeds those chunks a batch at a time, in windows,
before any unit of work opens, writes the vectors inside one, and leaves a
document the encoder may not see alone. A provider failure ends the round;
the next round tries again.
"""

from __future__ import annotations

import logging

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import (
    MemoryUnitOfWorkFactory,
    _unit_of_work_is_open,
)
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.domain.agents import Principal
from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest, EmbeddingResponse
from agent_core.domain.knowledge import KnowledgeSemanticQuery
from agent_core.domain.memory import Sensitivity
from agent_core.domain.semantic_memory import EmbeddingSpace
from agent_core.knowledge import embedding as embedding_module
from agent_core.knowledge.backfill import KnowledgeVectorBackfill
from agent_core.knowledge.embedding import KnowledgeEmbedder
from tests.contract.support import NOW, memory_stack, principal
from tests.contract.test_knowledge_passage_search_contract import _document, _query
from tests.contract.test_knowledge_semantic_contract import TEXT, _headings

SPACE = EmbeddingSpace(provider="fixture", model="semantic", revision="v1", dimensions=3)


class Provider:
    """Embeds the fan passage by the question and everything else away from it."""

    def __init__(self, *, failing: bool = False, fail_after: int | None = None) -> None:
        self.calls: list[EmbeddingRequest] = []
        self.failing = failing
        self.fail_after = fail_after

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        assert not _unit_of_work_is_open(), "backfill embeds before a unit of work opens"
        self.calls.append(request)
        if self.failing or (self.fail_after is not None and len(self.calls) > self.fail_after):
            raise EmbeddingError("local embedding capacity unavailable")
        vectors = tuple(
            (1.0, 0.0, 0.0) if "fan" in text.split("\n", 1)[-1] else (0.0, 1.0, 0.0)
            for text in request.texts
        )
        return EmbeddingResponse(
            request_digest=request.digest, vectors=vectors, input_tokens=len(vectors)
        )


def _words(text: str) -> int:
    return len(text.split())


async def _factory() -> tuple[MemoryUnitOfWorkFactory, InMemoryKnowledgeStore]:
    clock, sessions, runs, events = await memory_stack()
    repositories = _memory_uow_repositories(
        agents=InMemoryAgentRepository(),
        sessions=sessions,
        runs=runs,
        events=events,
        invocations=InMemoryToolInvocationRepository(runs),
        clock=clock,
    )
    factory = MemoryUnitOfWorkFactory(repositories)
    async with factory() as uow:
        store = uow.knowledge
    assert isinstance(store, InMemoryKnowledgeStore)
    return factory, store


def _backfill(
    factory: MemoryUnitOfWorkFactory,
    embedder: KnowledgeEmbedder,
    *,
    owner: Principal | None = None,
    batch: int = 64,
    group: int = 8,
) -> KnowledgeVectorBackfill:
    return KnowledgeVectorBackfill(
        uow_factory=factory,
        embedder=embedder,
        principal=owner or principal(),
        clock=FixedClock(NOW),
        batch_chunks=batch,
        group_chunks=group,
    )


async def test_each_group_is_written_before_the_next_is_embedded() -> None:
    """A round that ends early keeps what it embedded: progress survives a deadline."""

    factory, store = await _factory()
    await store.ingest(_document(1, TEXT))
    provider = Provider(fail_after=1)
    sweep = _backfill(factory, KnowledgeEmbedder(provider, SPACE), group=1)
    assert await sweep.sweep_once() == 1
    provider.fail_after = None
    assert await sweep.sweep_once() == 2
    assert await sweep.sweep_once() == 0


async def test_rounds_embed_waiting_chunks_in_windows_until_none_remain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory, store = await _factory()
    await store.ingest(_document(1, TEXT))
    await store.ingest(_document(2, TEXT))
    provider = Provider()
    embedder = KnowledgeEmbedder(
        provider, SPACE, channel="dense", window_tokens=6, count_tokens=_words
    )
    sweep = _backfill(factory, embedder, batch=4)
    with caplog.at_level(logging.INFO, logger="agent_core.knowledge.backfill"):
        assert await sweep.sweep_once() == 4
        assert await sweep.sweep_once() == 2
        assert await sweep.sweep_once() == 0
    windows = sum(len(call.texts) for call in provider.calls)
    assert windows > 6, "every chunk of the guide is wider than six words"
    rounds = [record for record in caplog.records if record.msg == "knowledge_vectors_backfilled"]
    assert (rounds[0].chunks, rounds[0].windows) == (4, len(provider.calls[0].texts))  # type: ignore[attr-defined]
    assert sum(record.chunks for record in rounds) == 6  # type: ignore[attr-defined]
    assert sum(record.windows for record in rounds) == windows  # type: ignore[attr-defined]
    question = KnowledgeSemanticQuery(space=SPACE, vector=(1.0, 0.0, 0.0), channel="dense")
    passages = await store.search(_query(text="lunch menu", max_per_document=1), semantic=question)
    assert _headings(passages) == ["Fans", "Fans"]
    assert all(item.arms == ["semantic"] for item in passages)


async def test_a_document_the_encoder_may_not_see_is_left_lexical() -> None:
    factory, store = await _factory()
    await store.ingest(_document(1, TEXT, sensitivity=Sensitivity.RESTRICTED))
    provider = Provider()
    remote = _backfill(factory, KnowledgeEmbedder(provider, SPACE, local=False))
    assert await remote.sweep_once() == 0
    assert provider.calls == []
    local = _backfill(factory, KnowledgeEmbedder(provider, SPACE, local=True))
    assert await local.sweep_once() == 3
    assert await local.sweep_once() == 0


async def test_a_provider_failure_ends_the_round_and_the_next_round_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory, store = await _factory()
    await store.ingest(_document(1, TEXT))
    provider = Provider(failing=True)
    sweep = _backfill(factory, KnowledgeEmbedder(provider, SPACE))
    with caplog.at_level(logging.WARNING, logger="agent_core.knowledge.backfill"):
        assert await sweep.sweep_once() == 0
    ended = [record for record in caplog.records if record.msg == "knowledge_vector_backfill_ended"]
    assert [record.error_class for record in ended] == ["EmbeddingError"]  # type: ignore[attr-defined]
    assert all("capacity" not in record.getMessage() for record in ended)
    provider.failing = False
    assert await sweep.sweep_once() == 3
    assert await sweep.sweep_once() == 0


async def test_a_withdrawn_admission_embeds_nothing() -> None:
    factory, store = await _factory()
    await store.ingest(_document(1, TEXT))
    provider = Provider()
    sweep = _backfill(factory, KnowledgeEmbedder(provider, SPACE, admitted=lambda: False))
    assert await sweep.sweep_once() == 0
    assert provider.calls == []


async def test_admission_withdrawn_mid_round_stops_before_the_next_group() -> None:
    """Admission is read before every provider call, not once a round."""

    factory, store = await _factory()
    await store.ingest(_document(1, TEXT))
    provider = Provider()
    admitted = [True]
    embedder = KnowledgeEmbedder(provider, SPACE, admitted=lambda: admitted[0])
    original = provider.embed

    async def embed_then_withdraw(request: EmbeddingRequest) -> EmbeddingResponse:
        response = await original(request)
        admitted[0] = False
        return response

    provider.embed = embed_then_withdraw  # type: ignore[method-assign]
    assert await _backfill(factory, embedder, group=1).sweep_once() == 1
    assert len(provider.calls) == 1


async def test_a_withdrawal_inside_a_group_writes_nothing_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group interrupted by a withdrawal is not written half-embedded."""

    monkeypatch.setattr(embedding_module, "EMBEDDING_BATCH", 1)
    factory, store = await _factory()
    await store.ingest(_document(1, TEXT))
    provider = Provider()
    admitted = [True]
    original = provider.embed

    async def embed_then_withdraw(request: EmbeddingRequest) -> EmbeddingResponse:
        response = await original(request)
        admitted[0] = False
        return response

    provider.embed = embed_then_withdraw  # type: ignore[method-assign]
    embedder = KnowledgeEmbedder(provider, SPACE, admitted=lambda: admitted[0])
    assert await _backfill(factory, embedder, group=8).sweep_once() == 0
    assert store._vectors == {}
    assert len(provider.calls) == 1


async def test_vectors_outside_the_active_space_are_pruned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A new space, from a new encoder or budget, replaces the old vectors rather than piling up.

    The old vectors go only once the active space holds every current chunk:
    until then a release still reading the old space, the previous API during
    a restart or a rollback, finds its vectors in place.
    """

    factory, store = await _factory()
    await store.ingest(_document(1, TEXT))
    old = EmbeddingSpace(provider="fixture", model="semantic", revision="v0", dimensions=3)
    old_embedder = KnowledgeEmbedder(Provider(), old)
    assert await _backfill(factory, old_embedder).sweep_once() == 3
    sweep = _backfill(factory, KnowledgeEmbedder(Provider(), SPACE), batch=2)
    with caplog.at_level(logging.INFO, logger="agent_core.knowledge.backfill"):
        assert await sweep.sweep_once() == 2
        assert all(old.key in spaces for spaces in store._vectors.values())
        assert await sweep.sweep_once() == 1
        assert all(old.key in spaces for spaces in store._vectors.values())
        assert await sweep.sweep_once() == 0
    assert all(old.key not in spaces for spaces in store._vectors.values())
    assert all(SPACE.key in spaces for spaces in store._vectors.values())
    pruned = [r for r in caplog.records if r.msg == "knowledge_vectors_pruned"]
    assert [r.vectors for r in pruned] == [3]  # type: ignore[attr-defined]


async def test_the_prune_leaves_other_owners_vectors_alone() -> None:
    """The sweep prunes only what its owner holds; another tenant's or principal's vectors stay."""

    factory, store = await _factory()
    old = EmbeddingSpace(provider="fixture", model="semantic", revision="v0", dimensions=3)
    old_embedder = KnowledgeEmbedder(Provider(), old)
    other_tenant = principal().model_copy(update={"tenant_id": "other-tenant"})
    other_principal = principal().model_copy(update={"principal_id": "other-principal"})
    tenant_document = _document(2, TEXT, tenant_id=other_tenant.tenant_id)
    prepared = _document(3, TEXT)
    principal_document = prepared.model_copy(
        update={
            "document": prepared.document.model_copy(
                update={"ingested_by_principal_id": other_principal.principal_id}
            )
        }
    )
    await store.ingest(_document(1, TEXT))
    await store.ingest(tenant_document)
    await store.ingest(principal_document)
    for owner in (None, other_tenant, other_principal):
        assert await _backfill(factory, old_embedder, owner=owner).sweep_once() == 3

    sweep = _backfill(factory, KnowledgeEmbedder(Provider(), SPACE))
    assert await sweep.sweep_once() == 3
    assert await sweep.sweep_once() == 0
    foreign = [*tenant_document.chunks, *principal_document.chunks]
    assert all(set(store._vectors[chunk.chunk_id]) == {old.key} for chunk in foreign)
    owned = _document(1, TEXT).chunks
    assert all(set(store._vectors[chunk.chunk_id]) == {SPACE.key} for chunk in owned)
