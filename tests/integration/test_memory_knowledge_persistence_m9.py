"""PostgreSQL round trips and isolation for Milestone 9 memory and knowledge."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import RandomIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import DEFAULT_AGENT_ID, Composition, build
from agent_core.config import Settings
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.events import NewEvent
from agent_core.domain.knowledge import (
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeVisibility,
)
from agent_core.domain.memory import BeliefType, RecallQuery
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Run, RunLimits, RunStatus
from tests.integration.m2_support import database_settings, memory_settings


async def _bytes(value: bytes) -> AsyncIterator[bytes]:
    yield value


async def test_postgres_memory_round_trip_across_compositions(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "memory-artifacts")
    marker = f"durable-memory-{uuid4()}"
    async with build(settings=settings, storage="postgres") as first:
        session_id = await first.sessions.create()
        async with first.uow_factory() as uow:
            source = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=first.principal.principal_id,
                    payload={"content": marker},
                )
            )
        belief = await first.memory.remember(
            session_id=session_id,
            run_id=None,
            statement=marker,
            subject=marker,
            scope="integration",
            belief_type=BeliefType.FACT,
            source_event_ids=[source.sequence],
        )

    async with build(settings=settings, storage="postgres") as second:
        recall_session = await second.sessions.create()
        result = await second.memory_retriever.recall(
            RecallQuery(
                tenant_id=second.principal.tenant_id,
                principal_id=second.principal.principal_id,
                current_scope="integration",
                text=marker,
                budget_tokens=500,
                max_items=5,
                min_score=0.1,
            ),
            session_id=recall_session,
        )
        assert [item.belief_id for item in result.items] == [belief.id]
        structured = await second.memory_retriever.recall(
            RecallQuery(
                tenant_id=second.principal.tenant_id,
                principal_id=second.principal.principal_id,
                current_scope="integration",
                text="no lexical match",
                subjects=[marker],
                budget_tokens=500,
                max_items=5,
                min_score=0.1,
            ),
            session_id=recall_session,
        )
        assert [item.belief_id for item in structured.items] == [belief.id]
        async with second.uow_factory() as uow:
            stored_trace = await uow.traces.get(result.trace_id, second.principal)
        assert stored_trace.rendered_sha256


async def test_postgres_knowledge_round_trip_and_visibility(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "knowledge-artifacts")
    marker = f"durableknowledge{uuid4().hex}"
    async with build(settings=settings, storage="postgres") as first:
        session_id = await first.sessions.create()
        run_id = uuid4()
        now = first.clock.now()
        async with first.uow_factory() as uow:
            await uow.runs.create(
                Run(
                    id=run_id,
                    session_id=session_id,
                    tenant_id=first.principal.tenant_id,
                    principal_scopes=set(first.principal.scopes),
                    agent_id=DEFAULT_AGENT_ID,
                    agent_version="1.0.0",
                    status=RunStatus.COMPLETED,
                    limits=RunLimits(),
                    scheduled_for=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        artifact_store = FilesystemArtifactStore(settings.artifact_root)
        writer = ArtifactWriterFactory(
            first.uow_factory,
            artifact_store,
            first.clock,
            RandomIdFactory(),
        ).for_run(
            tenant_id=first.principal.tenant_id,
            principal_id=first.principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            origin=ArtifactOrigin.UPLOAD,
        )
        ref = await writer.create(
            _bytes(f"{marker} is the durable operating marker.".encode()),
            "durable.txt",
            "text/plain",
            TrustLevel.USER,
        )
        async with first.uow_factory() as uow:
            source = await uow.artifacts.get(ref.artifact_id, first.principal)
        document = await first.knowledge.ingest(
            KnowledgeIngestRequest(
                source=source,
                title="Durable knowledge",
                visibility=KnowledgeVisibility.PRINCIPAL,
            ),
            origin_trust=TrustLevel.USER,
        )

    async with build(settings=settings, storage="postgres") as second:
        search_session = await second.sessions.create()
        turn_id = uuid4()
        query = KnowledgeQuery(
            tenant_id=second.principal.tenant_id,
            principal_id=second.principal.principal_id,
            current_scope=None,
            text=marker,
            budget_tokens=500,
            max_passages=3,
            min_score=0.1,
        )
        result = await second.knowledge.search(
            query,
            session_id=search_session,
            turn_id=turn_id,
        )
        assert result.passages[0].document_id == document.document_id
        isolated = await second.knowledge.search(
            query.model_copy(update={"principal_id": "another-principal"}),
            session_id=search_session,
        )
        assert isolated.passages == []
        await second.knowledge.delete(document.document_id)
        async with second.uow_factory() as uow:
            view = await uow.traces.user_view(
                turn_id,
                viewing_surface_id="private",
                viewing_ceiling="restricted",
            )
        assert view.passages[0].deleted is True
        assert view.passages[0].text is None


async def _ingested(composition: Composition, text: str) -> None:
    session_id = await composition.sessions.create()
    run_id = uuid4()
    now = composition.clock.now()
    async with composition.uow_factory() as uow:
        await uow.runs.create(
            Run(
                id=run_id,
                session_id=session_id,
                tenant_id=composition.principal.tenant_id,
                principal_scopes=set(composition.principal.scopes),
                agent_id=DEFAULT_AGENT_ID,
                agent_version="1.0.0",
                status=RunStatus.COMPLETED,
                limits=RunLimits(),
                scheduled_for=now,
                created_at=now,
                updated_at=now,
            )
        )
    assert composition.settings.artifact_root is not None
    ref = await (
        ArtifactWriterFactory(
            composition.uow_factory,
            FilesystemArtifactStore(composition.settings.artifact_root),
            composition.clock,
            RandomIdFactory(),
        )
        .for_run(
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            origin=ArtifactOrigin.UPLOAD,
        )
        .create(_bytes(text.encode()), "garden.md", "text/markdown", TrustLevel.USER)
    )
    async with composition.uow_factory() as uow:
        source = await uow.artifacts.get(ref.artifact_id, composition.principal)
    await composition.knowledge.ingest(
        KnowledgeIngestRequest(
            source=source, title="garden.md", visibility=KnowledgeVisibility.PRINCIPAL
        ),
        origin_trust=TrustLevel.USER,
    )


async def test_postgres_and_memory_knowledge_stores_agree_on_any_term_matching(
    tmp_path: Path,
) -> None:
    """A question retrieves a passage that shares only some of its words.

    knowledge.search receives the model's own phrasing, such as "when should I
    water the tomatoes", and a passage rarely holds every word of it. Both
    knowledge stores answer with the any-term semantics every lexical store
    shares (`lexical_query_terms`); the PostgreSQL store once required every
    word, so a real question found nothing in production while the in-memory
    store, which every gate uses, found the passage.
    """

    passage = "# Garden plan\n\nWater the tomatoes every morning before nine."
    found: dict[str, list[str]] = {}
    stores: tuple[tuple[Literal["memory", "postgres"], Settings], ...] = (
        ("memory", replace(memory_settings(), artifact_root=tmp_path / "memory")),
        ("postgres", replace(database_settings(), artifact_root=tmp_path / "postgres")),
    )
    for storage, settings in stores:
        async with build(settings=settings, storage=storage) as composition:
            await _ingested(composition, passage)
            result = await composition.knowledge.search(
                KnowledgeQuery(
                    tenant_id=composition.principal.tenant_id,
                    principal_id=composition.principal.principal_id,
                    current_scope=None,
                    text="When should I water the tomatoes?",
                    budget_tokens=2_000,
                    max_passages=3,
                    min_score=0.1,
                ),
                session_id=await composition.sessions.create(),
            )
            unrelated = await composition.knowledge.search(
                KnowledgeQuery(
                    tenant_id=composition.principal.tenant_id,
                    principal_id=composition.principal.principal_id,
                    current_scope=None,
                    text="quarterly revenue forecast",
                    budget_tokens=2_000,
                    max_passages=3,
                    min_score=0.1,
                ),
                session_id=await composition.sessions.create(),
            )
        assert unrelated.passages == [], storage
        found[storage] = [item.text for item in result.passages]

    assert found["memory"], found
    assert found["postgres"] == found["memory"]
