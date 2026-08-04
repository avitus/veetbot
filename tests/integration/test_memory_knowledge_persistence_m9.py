"""PostgreSQL round trips and isolation for Milestone 9 memory and knowledge."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import RandomIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import DEFAULT_AGENT_ID, build
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
from tests.integration.m2_support import database_settings


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
        query = KnowledgeQuery(
            tenant_id=second.principal.tenant_id,
            principal_id=second.principal.principal_id,
            current_scope=None,
            text=marker,
            budget_tokens=500,
            max_passages=3,
            min_score=0.1,
        )
        result = await second.knowledge.search(query, session_id=search_session)
        assert result.passages[0].document_id == document.document_id
        isolated = await second.knowledge.search(
            query.model_copy(update={"principal_id": "another-principal"}),
            session_id=search_session,
        )
        assert isolated.passages == []
