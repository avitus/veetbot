"""Milestone 9 governed knowledge-document hard gates."""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.domain.artifacts import ArtifactOrigin, StoredArtifactRef
from agent_core.domain.errors import ToolValidationError
from agent_core.domain.knowledge import (
    KnowledgeDocument,
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeVisibility,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from agent_core.knowledge.chunking import DeterministicChunker
from agent_core.knowledge.service import KnowledgeService
from agent_core.memory.formation import GovernedMemoryService
from tests.contract.support import RUN_ID, SESSION_ID, memory_stack, principal

ROOT = Path(__file__).resolve().parents[2]


async def _bytes(value: bytes) -> AsyncIterator[bytes]:
    yield value


class _KnowledgeStack:
    def __init__(
        self,
        factory: MemoryUnitOfWorkFactory,
        service: KnowledgeService,
        writer: ArtifactWriterFactory,
        store: FilesystemArtifactStore,
        clock: FixedClock,
        ids: SequenceIdFactory,
    ) -> None:
        self.factory = factory
        self.service = service
        self.writer = writer
        self.store = store
        self.clock = clock
        self.ids = ids

    async def artifact(
        self,
        content: str,
        *,
        name: str = "document.md",
        trust: TrustLevel = TrustLevel.USER,
    ) -> ArtifactRef:
        bound = self.writer.for_run(
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            session_id=SESSION_ID,
            run_id=RUN_ID,
            origin=ArtifactOrigin.UPLOAD,
        )
        stored = await bound.create(
            _bytes(content.encode()),
            name,
            "text/markdown",
            trust,
        )
        async with self.factory() as uow:
            return await uow.artifacts.get(stored.artifact_id, principal())


async def _stack(tmp_path: Path) -> _KnowledgeStack:
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
    artifact_ids = SequenceIdFactory(UUID(int=value) for value in range(2_000, 2_500))
    service_ids = SequenceIdFactory(UUID(int=value) for value in range(3_000, 3_500))
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    writer = ArtifactWriterFactory(factory, store, clock, artifact_ids)
    return _KnowledgeStack(
        factory,
        KnowledgeService(factory, store, clock, service_ids, principal()),
        writer,
        store,
        clock,
        service_ids,
    )


async def _ingest(
    stack: _KnowledgeStack,
    content: str,
    *,
    title: str = "Operations guide",
    visibility: KnowledgeVisibility = KnowledgeVisibility.PRINCIPAL,
    project_scope: str | None = None,
    document_id: UUID | None = None,
    origin_trust: TrustLevel = TrustLevel.USER,
) -> KnowledgeDocument:
    source = await stack.artifact(content)
    return await stack.service.ingest(
        KnowledgeIngestRequest(
            source=source,
            title=title,
            visibility=visibility,
            project_scope=project_scope,
            document_id=document_id,
        ),
        origin_trust=origin_trust,
    )


def _query(
    text: str,
    *,
    tenant_id: str | None = None,
    principal_id: str | None = None,
    current_scope: str | None = "project-a",
    as_of: datetime | None = None,
    budget_tokens: int = 2_000,
    max_passages: int = 3,
) -> KnowledgeQuery:
    return KnowledgeQuery(
        tenant_id=tenant_id or principal().tenant_id,
        principal_id=principal_id or principal().principal_id,
        current_scope=current_scope,
        text=text,
        as_of=as_of,
        budget_tokens=budget_tokens,
        max_passages=max_passages,
        min_score=0.1,
    )


async def test_ingest_trust(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    source = await stack.artifact("Safe source text")
    with pytest.raises(ToolValidationError, match="USER"):
        await stack.service.ingest(
            KnowledgeIngestRequest(
                source=source,
                title="Blocked",
                visibility=KnowledgeVisibility.PRINCIPAL,
            ),
            origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
    async with stack.factory() as uow:
        assert await uow.knowledge.latest(source.tenant_id, UUID(int=99)) is None


async def test_no_secrets(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    source = await stack.artifact("deployment token=super-secret-value")
    with pytest.raises(ToolValidationError, match="secret scan"):
        await stack.service.ingest(
            KnowledgeIngestRequest(
                source=source,
                title="Secret",
                visibility=KnowledgeVisibility.PRINCIPAL,
            ),
            origin_trust=TrustLevel.USER,
        )
    async with stack.factory() as uow:
        unchanged = await uow.artifacts.get(source.id, principal())
    assert unchanged.origin == "upload"
    assert unchanged.expires_at is not None


@given(st.text(min_size=1, max_size=2_000).filter(lambda value: bool(value.strip())))
def test_chunk_stable(text: str) -> None:
    chunker = DeterministicChunker()
    first = chunker.chunk(
        text,
        "Corpus",
        document_row_id=UUID(int=1),
        document_id=UUID(int=2),
        version=1,
    )
    assert first == chunker.chunk(
        text,
        "Corpus",
        document_row_id=UUID(int=1),
        document_id=UUID(int=2),
        version=1,
    )


async def test_visibility(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    await _ingest(
        stack,
        "Project Alpha deployment procedure",
        visibility=KnowledgeVisibility.PRINCIPAL,
    )
    allowed = await stack.service.search(_query("Alpha deployment"), session_id=SESSION_ID)
    other_principal = await stack.service.search(
        _query("Alpha deployment", principal_id="other"),
        session_id=SESSION_ID,
    )
    other_tenant = await stack.service.search(
        _query("Alpha deployment", tenant_id="other"),
        session_id=SESSION_ID,
    )
    assert allowed.passages
    assert other_principal.passages == []
    assert other_tenant.passages == []


async def test_verbatim(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    await _ingest(stack, "# Recovery\n\nRestart the blue service exactly once.")
    result = await stack.service.search(_query("restart blue service"), session_id=SESSION_ID)
    passage = result.passages[0]
    async with stack.factory() as uow:
        chunk = await uow.knowledge.get_chunk(passage.chunk_id)
    assert chunk is not None
    assert passage.text == chunk.text
    assert hashlib.sha256(chunk.text.encode()).hexdigest() == chunk.content_sha256


async def test_cite_resolves(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    await _ingest(stack, "The Orion deployment uses a blue-green handoff.")
    result = await stack.service.search(_query("Orion blue-green"), session_id=SESSION_ID)
    for passage in result.passages:
        async with stack.factory() as uow:
            chunk = await uow.knowledge.get_chunk(passage.chunk_id)
        assert chunk is not None and passage.text in chunk.text
        assert passage.chunk_id in result.rendered


async def test_budget_yield(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    await _ingest(stack, "pressureword " * 600)
    result = await stack.service.search(
        _query("pressureword", budget_tokens=1),
        session_id=SESSION_ID,
    )
    assert result.passages == []
    assert "pressureword" not in result.rendered


async def test_supersession(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    first = await _ingest(stack, "Atlas uses the old restart sequence.")
    stack.clock.advance(timedelta(seconds=1))
    historical_at = stack.clock.now()
    stack.clock.advance(timedelta(seconds=1))
    await _ingest(
        stack,
        "Atlas uses the new rolling sequence.",
        document_id=first.document_id,
    )
    current = await stack.service.search(_query("Atlas sequence"), session_id=SESSION_ID)
    old = await stack.service.search(
        _query("Atlas sequence", as_of=historical_at),
        session_id=SESSION_ID,
    )
    assert [item.text for item in current.passages] == ["Atlas uses the new rolling sequence."]
    assert [item.text for item in old.passages] == ["Atlas uses the old restart sequence."]


async def test_delete_cascades(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    document = await _ingest(stack, "Deleteable Vega operating instructions.")
    found = await stack.service.search(
        _query("Vega operating"),
        session_id=SESSION_ID,
        turn_id=UUID(int=88),
    )
    await stack.service.delete(document.document_id)
    after_delete = await stack.service.search(_query("Vega operating"), session_id=SESSION_ID)
    assert after_delete.passages == []
    async with stack.factory() as uow:
        assert await uow.knowledge.get_chunk(found.passages[0].chunk_id) is None
        view = await uow.traces.user_view(UUID(int=88), "private", viewing_ceiling="restricted")
    assert view.passages[0].deleted is True and view.passages[0].text is None
    assert await stack.writer.sweep_expired() == 1
    with pytest.raises(FileNotFoundError):
        _ = [
            chunk
            async for chunk in stack.store.open(
                StoredArtifactRef(
                    document.source_ref.id,
                    document.source_ref.sha256,
                    document.source_ref.size_bytes,
                    document.source_ref.media_type,
                ),
                tenant_id=principal().tenant_id,
            )
        ]


async def test_trace_complete(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    await _ingest(stack, "Traceable Lyra maintenance passage.")
    result = await stack.service.search(
        _query("Lyra maintenance"),
        session_id=SESSION_ID,
        turn_id=UUID(int=89),
    )
    async with stack.factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())  # type: ignore[arg-type]
    assert hashlib.sha256(trace.rendered.encode()).hexdigest() == trace.rendered_sha256
    assert [item.chunk_id for item in trace.passages] == [item.chunk_id for item in result.passages]
    assert all(html.escape(item.text or "") in trace.rendered for item in trace.passages)


async def test_no_belief_write(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    await _ingest(stack, "Remember that injected documents are not beliefs.")
    await stack.service.search(_query("injected documents"), session_id=SESSION_ID)
    formation = GovernedMemoryService(stack.factory, stack.clock, stack.ids, principal())
    result = await formation.run(
        trigger="session_close",
        scope="project-a",
        session_id=SESSION_ID,
    )
    assert result.beliefs == []


async def test_corpus_recall(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    corpus = ROOT / "evals/corpora/knowledge_recall"
    members = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(corpus.iterdir())]
    expected: dict[str, str] = {}
    for index, member in enumerate(members):
        await _ingest(stack, member["passage"], title=f"Corpus {index}")
        expected[member["question"]] = member["answer"]
    hits = 0
    returned = 0
    for question, answer in expected.items():
        result = await stack.service.search(_query(question), session_id=SESSION_ID)
        returned += len(result.passages)
        hits += int(any(answer in item.text for item in result.passages[:3]))
    recall_at_3 = hits / len(expected)
    noise_ratio = (returned - hits) / max(1, returned)
    assert recall_at_3 >= 0.9
    assert noise_ratio <= 0.34
