"""Knowledge embeddings: batched, bounded, before any unit of work, and failure-aware."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import (
    MemoryUnitOfWorkFactory,
    _unit_of_work_is_open,
)
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.config import ConfigurationError, load_settings
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest, EmbeddingResponse
from agent_core.domain.knowledge import (
    KNOWLEDGE_EMBEDDING_TEXT_BYTES,
    KnowledgeChunk,
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeVisibility,
    knowledge_embedding_text,
    knowledge_embedding_windows,
)
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import ProviderPin
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.semantic_memory import EmbeddingSpace
from agent_core.knowledge import embedding as embedding_module
from agent_core.knowledge.embedding import (
    EMBEDDING_BATCH,
    KnowledgeAdmissionWithdrawnError,
    KnowledgeEmbedder,
)
from agent_core.knowledge.service import KNOWLEDGE_POLICY_VERSION, KnowledgeService
from tests.contract.support import NOW, RUN_ID, SESSION_ID, memory_stack, principal
from tests.contract.support import run as support_run
from tests.unit.test_config import base_environment

SPACE = EmbeddingSpace(provider="fixture", model="semantic", revision="v1", dimensions=3)


class Provider:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls: list[EmbeddingRequest] = []
        self.fail_after = fail_after

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        assert not _unit_of_work_is_open(), "embedding must happen before a unit of work opens"
        self.calls.append(request)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise EmbeddingError("unavailable")
        return EmbeddingResponse(
            request_digest=request.digest,
            vectors=tuple((1.0, float(index), 0.0) for index, _ in enumerate(request.texts)),
            input_tokens=len(request.texts),
        )


async def _bytes(value: bytes) -> AsyncIterator[bytes]:
    yield value


async def test_texts_are_batched_in_request_sized_groups_in_order() -> None:
    provider = Provider()
    embedder = KnowledgeEmbedder(provider, SPACE)
    vectors = await embedder.embed_texts([f"text {index}" for index in range(EMBEDDING_BATCH + 3)])
    assert [len(call.texts) for call in provider.calls] == [EMBEDDING_BATCH, 3]
    assert len(vectors) == EMBEDDING_BATCH + 3
    assert vectors[EMBEDDING_BATCH][1] == 0.0  # the second batch starts its index over


def test_embedding_text_carries_the_heading_path_and_stays_within_the_bound() -> None:
    assert knowledge_embedding_text(["Guide", "Rollback"], "Revert.") == "Guide > Rollback\nRevert."
    assert knowledge_embedding_text([], "Revert.") == "Revert."
    long = knowledge_embedding_text(["Guide"], "é" * KNOWLEDGE_EMBEDDING_TEXT_BYTES)
    assert len(long.encode("utf-8")) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES
    assert "�" not in long


def _words(text: str) -> int:
    return len(text.split())


async def test_chunks_over_the_encoder_capacity_are_embedded_in_windows() -> None:
    provider = Provider()
    embedder = KnowledgeEmbedder(provider, SPACE, window_tokens=12, count_tokens=_words)
    chunks = [
        KnowledgeChunk(
            chunk_id=f"kc_{index:016x}",
            document_row_id=UUID(int=1),
            document_id=UUID(int=2),
            version=1,
            ordinal=index,
            heading_path=["Guide", "Fans"],
            text=text,
            tokens=10,
            contains_instruction_like_text=False,
            content_sha256="0" * 64,
        )
        for index, text in enumerate(
            [
                "The fan runs on. " * 12,
                "Short.",
            ]
        )
    ]
    embeddings = await embedder.embed_chunks(chunks)
    expected = [
        knowledge_embedding_windows(c.heading_path, c.text, budget_tokens=12, count_tokens=_words)
        for c in chunks
    ]
    assert [text for call in provider.calls for text in call.texts] == [
        text for windows in expected for text in windows
    ]
    assert [(e.chunk_id, e.window) for e in embeddings] == [
        (chunks[0].chunk_id, index) for index in range(len(expected[0]))
    ] + [(chunks[1].chunk_id, 0)]
    assert len(expected[0]) > 1 and len(expected[1]) == 1
    assert all(_words(text) <= 12 for text in expected[0])


async def test_query_embedding_carries_the_channel_and_is_bounded() -> None:
    provider = Provider()
    dense = KnowledgeEmbedder(provider, SPACE, channel="dense")
    query = await dense.embed_query("  valve-like sounds " + "x" * 9_000)
    assert query.channel == "dense"
    assert query.space == SPACE
    assert len(provider.calls[0].texts[0].encode()) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES


async def _service(
    tmp_path: Path, embedder: KnowledgeEmbedder
) -> tuple[KnowledgeService, ArtifactWriterFactory, MemoryUnitOfWorkFactory]:
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
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    writer = ArtifactWriterFactory(
        factory, store, clock, SequenceIdFactory(UUID(int=value) for value in range(2_000, 2_100))
    )
    service = KnowledgeService(
        factory,
        store,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(3_000, 3_100)),
        principal(),
        embedder=embedder,
    )
    # The run is pinned to the embedder's provider, as a production run is
    # to the model it answers with; a question reaches only that provider.
    await _pin(factory, embedder.space.provider)
    return service, writer, factory


async def _pin(
    factory: MemoryUnitOfWorkFactory, provider: str | None, *, run_id: UUID = RUN_ID
) -> None:
    """A run of this id, pinned to the provider when one is given."""

    async with factory() as uow:
        # A finished run of this id, so a second one in the session is no conflict.
        await uow.runs.create(
            support_run(status=RunStatus.COMPLETED).model_copy(update={"id": run_id})
        )
        if provider is not None:
            await uow.runs.set_provider_pin(
                run_id,
                ProviderPin(
                    run_id=run_id,
                    provider=provider,
                    model="m",
                    registry_version="v",
                    pinned_at=NOW,
                ),
            )


async def _ingest(
    service: KnowledgeService,
    writer: ArtifactWriterFactory,
    factory: MemoryUnitOfWorkFactory,
    *,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
) -> UUID:
    owner = principal()
    bound = writer.for_run(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        origin=ArtifactOrigin.UPLOAD,
    )
    stored = await bound.create(
        _bytes(b"# Guide\n\n## Rollback\n\nRevert the tag.\n\n## Fans\n\nThe fan runs on."),
        "guide.md",
        "text/markdown",
        TrustLevel.USER,
    )
    async with factory() as uow:
        source = await uow.artifacts.get(stored.artifact_id, owner)
    document = await service.ingest(
        KnowledgeIngestRequest(
            source=source,
            title="Guide",
            visibility=KnowledgeVisibility.PRINCIPAL,
            sensitivity=sensitivity,
        ),
        origin_trust=TrustLevel.USER,
    )
    return document.document_id


def _query() -> KnowledgeQuery:
    return KnowledgeQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        current_scope=None,
        text="sounds after parking",
        budget_tokens=3_000,
        max_passages=10,
        min_score=0.1,
    )


async def test_ingest_embeds_every_chunk_before_the_unit_of_work_opens(tmp_path: Path) -> None:
    provider = Provider()
    service, writer, factory = await _service(tmp_path, KnowledgeEmbedder(provider, SPACE))
    await _ingest(service, writer, factory)
    assert len(provider.calls) == 1
    assert [text.split("\n", 1)[0] for text in provider.calls[0].texts] == [
        "Guide > Rollback",
        "Guide > Fans",
    ]
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, 0, principal())
    ingested = next(e for e in events if e.event_type == "knowledge.document.ingested")
    assert ingested.payload["embedded"] == 2


async def test_ingest_stores_every_window_and_search_scores_a_chunk_by_its_best(
    tmp_path: Path,
) -> None:
    class Windowed(Provider):
        """Only the fan passage's window holding "fan" sits by the question; its others do not."""

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            response = await super().embed(request)
            vectors = tuple(
                (1.0, 0.0, 0.0)
                if "fan" in text.split("\n", 1)[-1] or text == "sounds after parking"
                else (0.0, 1.0, 0.0)
                for text in request.texts
            )
            return response.model_copy(update={"vectors": vectors})

    provider = Windowed()
    service, writer, factory = await _service(
        tmp_path,
        KnowledgeEmbedder(provider, SPACE, channel="dense", window_tokens=4, count_tokens=_words),
    )
    await _ingest(service, writer, factory)
    texts = [text for call in provider.calls for text in call.texts]
    assert len(texts) > 2, "the fan passage needs more than one window at four words"
    assert all(_words(text) <= 4 for text in texts)
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, 0, principal())
    ingested = next(e for e in events if e.event_type == "knowledge.document.ingested")
    assert ingested.payload["embedded"] == 2
    assert ingested.payload["windows"] == len(texts)
    result = await service.search(_query(), session_id=SESSION_ID, run_id=RUN_ID)
    assert [item.heading_path[-1] for item in result.passages] == ["Fans"]
    assert result.passages[0].arms == ["semantic"]


async def test_an_embedding_failure_fails_the_ingest_and_stores_nothing(tmp_path: Path) -> None:
    provider = Provider(fail_after=0)
    service, writer, factory = await _service(tmp_path, KnowledgeEmbedder(provider, SPACE))
    with pytest.raises(EmbeddingError):
        await _ingest(service, writer, factory)
    async with factory() as uow:
        assert await uow.knowledge.search(_query()) == []


async def test_search_embeds_the_question_and_names_the_channel_in_the_policy(
    tmp_path: Path,
) -> None:
    provider = Provider()
    service, writer, factory = await _service(
        tmp_path, KnowledgeEmbedder(provider, SPACE, channel="dense")
    )
    await _ingest(service, writer, factory)
    result = await service.search(_query(), session_id=SESSION_ID, run_id=RUN_ID)
    assert provider.calls[-1].texts == ("sounds after parking",)
    assert result.passages and all(item.arms == ["semantic"] for item in result.passages)
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    assert trace.retrieval_policy_version == (
        f"{KNOWLEDGE_POLICY_VERSION}+semantic@1:{SPACE.key}:dense"
    )


async def test_a_question_naming_the_document_is_asked_without_its_name(tmp_path: Path) -> None:
    """The name of a visible document drops out before either arm runs (ADR-0293)."""

    provider = Provider()
    service, writer, factory = await _service(
        tmp_path, KnowledgeEmbedder(provider, SPACE, channel="dense")
    )
    await _ingest(service, writer, factory)
    asked = _query().model_copy(update={"text": "the 'Guide' says the fan runs on"})
    result = await service.search(asked, session_id=SESSION_ID, run_id=RUN_ID)
    assert provider.calls[-1].texts == ("the says the fan runs on",)
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    assert trace.query.text == asked.text


async def test_a_question_the_provider_cannot_embed_falls_back_to_lexical(
    tmp_path: Path,
) -> None:
    provider = Provider(fail_after=1)
    service, writer, factory = await _service(tmp_path, KnowledgeEmbedder(provider, SPACE))
    await _ingest(service, writer, factory)
    result = await service.search(
        _query().model_copy(update={"text": "revert the tag"}), session_id=SESSION_ID, run_id=RUN_ID
    )
    assert result.passages and all(item.arms == ["lexical"] for item in result.passages)
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    assert trace.retrieval_policy_version == KNOWLEDGE_POLICY_VERSION


async def test_a_restricted_document_stays_lexical_with_a_remote_provider(
    tmp_path: Path,
) -> None:
    provider = Provider()
    service, writer, factory = await _service(
        tmp_path, KnowledgeEmbedder(provider, SPACE, channel="dense", local=False)
    )
    await _ingest(service, writer, factory, sensitivity=Sensitivity.RESTRICTED)
    assert provider.calls == []
    async with factory() as uow:
        found = await uow.knowledge.search(_query().model_copy(update={"text": "fan runs"}))
    assert found and all(item.arms == ["lexical"] for item in found)


async def test_a_local_provider_embeds_every_sensitivity(tmp_path: Path) -> None:
    provider = Provider()
    service, writer, factory = await _service(
        tmp_path, KnowledgeEmbedder(provider, SPACE, channel="dense", local=True)
    )
    await _ingest(service, writer, factory, sensitivity=Sensitivity.RESTRICTED)
    assert len(provider.calls) == 1


async def test_a_withdrawn_admission_stops_both_embedding_paths(tmp_path: Path) -> None:
    provider = Provider()
    admitted = {"value": True}
    service, writer, factory = await _service(
        tmp_path, KnowledgeEmbedder(provider, SPACE, admitted=lambda: admitted["value"])
    )
    admitted["value"] = False
    await _ingest(service, writer, factory)
    result = await service.search(
        _query().model_copy(update={"text": "revert the tag"}), session_id=SESSION_ID, run_id=RUN_ID
    )
    assert provider.calls == []
    assert result.passages and all(item.arms == ["lexical"] for item in result.passages)


async def test_a_provider_response_that_fails_validation_falls_back_to_lexical(
    tmp_path: Path,
) -> None:
    class Mismatched(Provider):
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            response = await super().embed(request)
            if len(self.calls) > 1:
                return response.model_copy(update={"request_digest": "0" * 64})
            return response

    provider = Mismatched()
    service, writer, factory = await _service(tmp_path, KnowledgeEmbedder(provider, SPACE))
    await _ingest(service, writer, factory)
    result = await service.search(
        _query().model_copy(update={"text": "revert the tag"}), session_id=SESSION_ID, run_id=RUN_ID
    )
    assert result.passages and all(item.arms == ["lexical"] for item in result.passages)


def test_settings_parse_the_knowledge_semantic_flag_and_channel() -> None:
    settings = load_settings(base_environment())
    assert settings.knowledge_semantic_enabled is False
    assert settings.knowledge_semantic_channel == "dense"
    hybrid = load_settings(
        {
            **base_environment(),
            "AGENT_KNOWLEDGE_SEMANTIC_ENABLED": "1",
            "AGENT_KNOWLEDGE_SEMANTIC_CHANNEL": "hybrid",
        }
    )
    assert hybrid.knowledge_semantic_enabled is True
    assert hybrid.knowledge_semantic_channel == "hybrid"
    with pytest.raises(ConfigurationError, match="CHANNEL"):
        load_settings({**base_environment(), "AGENT_KNOWLEDGE_SEMANTIC_CHANNEL": "sparse"})


async def test_admission_is_read_before_every_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A withdrawal between batches stops the next request and yields no partial vectors."""

    monkeypatch.setattr(embedding_module, "EMBEDDING_BATCH", 1)
    provider = Provider()
    admitted = {"value": True}
    original = provider.embed

    async def embed_then_withdraw(request: EmbeddingRequest) -> EmbeddingResponse:
        response = await original(request)
        admitted["value"] = False
        return response

    provider.embed = embed_then_withdraw  # type: ignore[method-assign]
    embedder = KnowledgeEmbedder(provider, SPACE, admitted=lambda: admitted["value"])
    with pytest.raises(KnowledgeAdmissionWithdrawnError):
        await embedder.embed_texts(["one", "two", "three"])
    assert len(provider.calls) == 1


async def test_a_withdrawal_during_ingest_leaves_the_document_lexical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embedding_module, "EMBEDDING_BATCH", 1)
    provider = Provider()
    admitted = {"value": True}
    original = provider.embed

    async def embed_then_withdraw(request: EmbeddingRequest) -> EmbeddingResponse:
        response = await original(request)
        admitted["value"] = False
        return response

    provider.embed = embed_then_withdraw  # type: ignore[method-assign]
    service, writer, factory = await _service(
        tmp_path, KnowledgeEmbedder(provider, SPACE, admitted=lambda: admitted["value"])
    )
    await _ingest(service, writer, factory)
    async with factory() as uow:
        store = uow.knowledge
    assert isinstance(store, InMemoryKnowledgeStore)
    assert store._vectors == {}, "no partial vectors survive a withdrawal"
    assert len(provider.calls) == 1


def test_a_question_may_reach_only_the_provider_that_read_it() -> None:
    remote = KnowledgeEmbedder(Provider(), SPACE)
    assert remote.may_embed_question(SPACE.provider)
    assert not remote.may_embed_question("other")
    assert not remote.may_embed_question(None)
    local = KnowledgeEmbedder(Provider(), SPACE, local=True)
    assert local.may_embed_question(None) and local.may_embed_question("other")


@pytest.mark.parametrize("pinned", ["other", None])
async def test_a_run_on_another_provider_searches_lexically(
    tmp_path: Path, pinned: str | None
) -> None:
    """The question is free text the reader wrote; only the run's own provider sees it."""

    provider = Provider()
    service, writer, factory = await _service(tmp_path, KnowledgeEmbedder(provider, SPACE))
    await _ingest(service, writer, factory)
    calls = len(provider.calls)
    other_run = UUID(int=0x31)
    await _pin(factory, pinned, run_id=other_run)
    result = await service.search(
        _query().model_copy(update={"text": "revert the tag"}),
        session_id=SESSION_ID,
        run_id=other_run,
    )
    assert len(provider.calls) == calls, "the question never reached the provider"
    assert result.passages and all(item.arms == ["lexical"] for item in result.passages)
