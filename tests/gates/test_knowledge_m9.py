"""Milestone 9 governed knowledge-document hard gates."""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import AsyncIterator
from dataclasses import replace
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
from agent_core.domain.agents import Principal
from agent_core.domain.artifacts import ArtifactOrigin, StoredArtifactRef
from agent_core.domain.errors import NotFoundError, ToolValidationError
from agent_core.domain.knowledge import (
    KnowledgeDocument,
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeVisibility,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from agent_core.knowledge.chunking import (
    MAX_KNOWLEDGE_SOURCE_BYTES,
    DeterministicChunker,
    PlainTextExtractor,
)
from agent_core.knowledge.service import KnowledgeService
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.profiles import TraceProfile
from agent_core.tools.knowledge_ingest import KnowledgeIngestTool
from tests.contract.support import RUN_ID, SESSION_ID, memory_stack, principal, tool_context

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
        owner: Principal | None = None,
    ) -> ArtifactRef:
        effective_owner = owner or principal()
        bound = self.writer.for_run(
            tenant_id=effective_owner.tenant_id,
            principal_id=effective_owner.principal_id,
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
            return await uow.artifacts.get(stored.artifact_id, effective_owner)


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
    rejected_document_id = UUID(int=99)
    with pytest.raises(ToolValidationError, match="USER"):
        await stack.service.ingest(
            KnowledgeIngestRequest(
                source=source,
                title="Blocked",
                visibility=KnowledgeVisibility.PRINCIPAL,
                document_id=rejected_document_id,
            ),
            origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
    async with stack.factory() as uow:
        assert await uow.knowledge.latest(source.tenant_id, rejected_document_id) is None


async def test_extractor_rejects_malformed_or_oversized_content() -> None:
    with pytest.raises(ToolValidationError, match="UTF-8"):
        await PlainTextExtractor().extract(_bytes(b"\xff"), "text/plain")
    with pytest.raises(ToolValidationError, match="byte ceiling"):
        await PlainTextExtractor(maximum_bytes=3).extract(_bytes(b"four"), "text/plain")


async def test_ingest_rejects_oversized_metadata_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = await _stack(tmp_path)
    opened = False

    async def unexpected_open(*_args: object, **_kwargs: object) -> AsyncIterator[bytes]:
        nonlocal opened
        opened = True
        raise AssertionError("oversized source was opened")

    monkeypatch.setattr(stack.store, "open_verified", unexpected_open)
    source = (await stack.artifact("small")).model_copy(
        update={"size_bytes": MAX_KNOWLEDGE_SOURCE_BYTES + 1}
    )
    with pytest.raises(ToolValidationError, match="byte ceiling"):
        await stack.service.ingest(
            KnowledgeIngestRequest(
                source=source,
                title="Oversized",
                visibility=KnowledgeVisibility.PRINCIPAL,
            ),
            origin_trust=TrustLevel.USER,
        )
    assert opened is False


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
    assert first
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
    async with stack.factory() as uow:
        isolated_trace = await uow.traces.get(other_principal.trace_id, principal())
    assert isolated_trace.principal_id == principal().principal_id


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
    assert result.passages
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


async def test_ingest_tool_retry_converges_on_one_version(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    source = await stack.artifact("Idempotent Vega operating instructions.")
    tool = KnowledgeIngestTool(stack.service, stack.factory)
    arguments = {
        "artifact_id": str(source.id),
        "title": "Vega guide",
        "visibility": KnowledgeVisibility.PRINCIPAL.value,
    }

    first = await tool.execute(arguments, tool_context())
    second = await tool.execute(arguments, tool_context())

    assert first.structured is not None
    assert second.structured is not None
    assert first.structured == second.structured
    assert first.structured["version"] == 1


async def test_ingest_tool_cannot_replace_another_principals_document(tmp_path: Path) -> None:
    stack = await _stack(tmp_path)
    document_id = UUID(int=8_001)
    owner_source = await stack.artifact("Owner-controlled Vega instructions.")
    owner_tool = KnowledgeIngestTool(stack.service, stack.factory)
    await owner_tool.execute(
        {
            "artifact_id": str(owner_source.id),
            "title": "Owner guide",
            "visibility": KnowledgeVisibility.TENANT.value,
            "document_id": str(document_id),
        },
        tool_context(),
    )

    other = principal().model_copy(update={"principal_id": "other-principal"})
    other_source = await stack.artifact("Unauthorized replacement.", owner=other)
    other_service = KnowledgeService(
        stack.factory,
        stack.store,
        stack.clock,
        stack.ids,
        other,
    )
    other_tool = KnowledgeIngestTool(other_service, stack.factory)
    other_context = replace(tool_context(), principal=other)

    with pytest.raises(NotFoundError, match="knowledge document not found"):
        await other_tool.execute(
            {
                "artifact_id": str(other_source.id),
                "title": "Owner guide",
                "visibility": KnowledgeVisibility.TENANT.value,
                "document_id": str(document_id),
            },
            other_context,
        )
    async with stack.factory() as uow:
        latest = await uow.knowledge.latest(principal().tenant_id, document_id)
    assert latest is not None
    assert latest.version == 1
    assert latest.ingested_by_principal_id == principal().principal_id


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
        trace = await uow.traces.get(result.trace_id, principal())
    assert hashlib.sha256(trace.rendered.encode()).hexdigest() == trace.rendered_sha256
    assert [item.chunk_id for item in trace.passages] == [item.chunk_id for item in result.passages]
    assert trace.passages
    assert all(item.text is not None for item in trace.passages)
    assert all(html.escape(item.text) in trace.rendered for item in trace.passages if item.text)


async def test_knowledge_trace_retention_reads_the_trace_profile(tmp_path: Path) -> None:
    """One retention number governs both retrieval paths that write traces."""

    stack = await _stack(tmp_path)
    service = KnowledgeService(
        stack.factory,
        stack.store,
        stack.clock,
        stack.ids,
        principal(),
        trace_retention=TraceProfile(operator_retention_days=7),
    )
    await _ingest(stack, "Retention-scoped Lyra maintenance passage.")
    result = await service.search(
        _query("Lyra maintenance"),
        session_id=SESSION_ID,
        turn_id=UUID(int=91),
    )
    async with stack.factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    assert trace.operator_fields_expire_at == stack.clock.now() + timedelta(days=7)


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
    noise = 0
    for question, answer in expected.items():
        result = await stack.service.search(_query(question), session_id=SESSION_ID)
        top = result.passages[:3]
        returned += len(top)
        hits += int(any(answer in item.text for item in top))
        noise += sum(1 for item in top if answer not in item.text)
    recall_at_3 = hits / len(expected)
    noise_ratio = noise / max(1, returned)
    assert recall_at_3 >= 0.9
    assert noise_ratio <= 0.34
