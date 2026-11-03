"""Knowledge semantic arm: a paraphrased question reaches its passage (gate 13).

knowledge-documents.md ("Indexing", hard gate 13) admits a `pgvector` arm only
when a corpus gate shows lift over lexical retrieval. The corpus holds
documents whose answering passage paraphrases the question the way an owner's
manual does: the question says "valve-like sounds" and "parked", the passage
says "audible noise" and "engine is shut off". The vectors are a real model's,
recorded once (`scripts/record_knowledge_embeddings.py`) and replayed here, so
`make check` measures real geometry without a provider call.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.knowledge import (
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeSemanticChannel,
    KnowledgeVisibility,
    RetrievedPassage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.knowledge.embedding import KnowledgeEmbedder
from agent_core.knowledge.service import KnowledgeService
from tests.contract.knowledge_embedding_fixture import (
    FIXTURE,
    LOCAL_FIXTURE,
    RecordedEmbeddingProvider,
)
from tests.contract.support import RUN_ID, SESSION_ID, memory_stack, principal

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "evals/corpora/knowledge_paraphrase"
# The OpenAI space the arm runs in (ADR-0290), and the memory index's local
# encoder (ADR-0188's MiniLM tuple), recorded with its windows and kept as the
# measured miss that sent the arm to its own provider.
SPACES = pytest.mark.parametrize("fixture", [FIXTURE, LOCAL_FIXTURE], ids=["openai", "onnx"])


async def _bytes(value: bytes) -> AsyncIterator[bytes]:
    yield value


def _members() -> list[dict[str, str]]:
    members = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(CORPUS.glob("*.json"))
    ]
    assert len(members) >= 3, "the paraphrase corpus must not pass vacuously"
    return members


async def _service(
    tmp_path: Path, embedder: KnowledgeEmbedder | None
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
        factory, store, clock, SequenceIdFactory(UUID(int=value) for value in range(2_000, 2_500))
    )
    service = KnowledgeService(
        factory,
        store,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(3_000, 3_500)),
        principal(),
        embedder=embedder,
    )
    return service, writer, factory


async def _ingest(
    service: KnowledgeService,
    writer: ArtifactWriterFactory,
    factory: MemoryUnitOfWorkFactory,
    member: dict[str, str],
) -> None:
    owner = principal()
    bound = writer.for_run(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        origin=ArtifactOrigin.UPLOAD,
    )
    stored = await bound.create(
        _bytes(member["document"].encode()), "manual.md", "text/markdown", TrustLevel.USER
    )
    async with factory() as uow:
        source = await uow.artifacts.get(stored.artifact_id, owner)
    await service.ingest(
        KnowledgeIngestRequest(
            source=source, title=member["title"], visibility=KnowledgeVisibility.PRINCIPAL
        ),
        origin_trust=TrustLevel.USER,
    )


def _query(text: str) -> KnowledgeQuery:
    # The `knowledge.search` tool's own parameters, so the gate measures the
    # ranking a chat question receives, two passages per document included.
    return KnowledgeQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        current_scope=None,
        text=text,
        budget_tokens=3_000,
        max_passages=10,
        max_per_document=2,
        min_score=0.1,
    )


def _rank(passages: list[RetrievedPassage], answer: str) -> int | None:
    return next((index for index, item in enumerate(passages, 1) if answer in item.text), None)


async def _ranks(
    tmp_path: Path, channel: KnowledgeSemanticChannel | None, fixture: Path = FIXTURE
) -> dict[str, int | None]:
    """The answering passage's rank per corpus member under one configuration."""

    embedder = None
    if channel is not None:
        embedder = RecordedEmbeddingProvider(fixture).embedder(channel=channel)
    service, writer, factory = await _service(tmp_path, embedder)
    members = _members()
    for member in members:
        await _ingest(service, writer, factory, member)
    ranks: dict[str, int | None] = {}
    for member in members:
        result = await service.search(_query(member["question"]), session_id=SESSION_ID)
        ranks[member["title"]] = _rank(result.passages, member["answer"])
    return ranks


async def test_paraphrase_lift(tmp_path: Path) -> None:
    """With the semantic arm on, every answering passage is in the top two."""

    ranks = await _ranks(tmp_path, "dense", FIXTURE)
    misses = {title: rank for title, rank in ranks.items() if rank is None or rank > 2}
    assert not misses, misses


async def test_the_local_encoder_does_not_reach_the_top_two(tmp_path: Path) -> None:
    """The measured reason the arm has its own provider (ADR-0290).

    The memory index's local encoder scores by shared words: a section that
    repeats "click" or "tray" outranks the passage that answers, at its full
    256-token window and at every smaller window budget tried, so two of the
    three members miss. Recorded so a change of local encoder is measured
    rather than assumed to lift.
    """

    ranks = await _ranks(tmp_path, "dense", LOCAL_FIXTURE)
    assert sum(1 for rank in ranks.values() if rank is None or rank > 2) >= 2, ranks


async def test_lexical_alone_misses_the_paraphrase(tmp_path: Path) -> None:
    """The baseline the lift is measured against: lexical search never reaches them.

    Each answering passage shares no content word with its question, so the
    coverage ranking (ADR-0285) places the document's other sections first and
    the two-per-document cap cuts the passage. This is the production miss the
    gate reproduces; if lexical retrieval ever closes it, the lift above must
    be re-measured rather than assumed.
    """

    ranks = await _ranks(tmp_path, None)
    assert all(rank is None or rank > 2 for rank in ranks.values()), ranks


async def test_hybrid_fusion_does_not_reach_the_top_two(tmp_path: Path) -> None:
    """The measured reason the activation condition names the dense channel.

    Reciprocal rank fusion with the lexical arm rewards a passage both arms
    rank: the document's other sections sit first lexically (function words
    count as coverage) and near the top semantically, so the answering
    passage never beats them (ADR-0289). Recorded so a change in either arm
    that lifts hybrid fusion is seen rather than guessed.
    """

    ranks = await _ranks(tmp_path, "hybrid", FIXTURE)
    assert all(rank is None or rank > 2 for rank in ranks.values()), ranks


@SPACES
async def test_an_unrelated_question_returns_no_passage(tmp_path: Path, fixture: Path) -> None:
    """The semantic floor: a question none of the documents answers finds nothing.

    The lexical arm returns only chunks holding a query term; the semantic arm
    returns only chunks above `KNOWLEDGE_SEMANTIC_MIN_SIMILARITY`, so the
    dense channel does not answer a question about tax returns with the
    nearest page of a car manual.
    """

    embedder = RecordedEmbeddingProvider(fixture).embedder(channel="dense")
    service, writer, factory = await _service(tmp_path, embedder)
    members = _members()
    for member in members:
        await _ingest(service, writer, factory, member)
    for member in members:
        result = await service.search(_query(member["unrelated_question"]), session_id=SESSION_ID)
        assert result.passages == [], (member["unrelated_question"], result.passages)
