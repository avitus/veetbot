"""The knowledge semantic arm on migrated PostgreSQL: pgvector rows, both channels, the cascade."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import RandomIdFactory
from agent_core.adapters.persistence.database import create_engine
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import DEFAULT_AGENT_ID, build
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse
from agent_core.domain.knowledge import (
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeSemanticChannel,
    KnowledgeVisibility,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Run, RunLimits, RunStatus
from agent_core.domain.semantic_memory import EmbeddingSpace
from agent_core.knowledge.backfill import KnowledgeVectorBackfill
from agent_core.knowledge.embedding import KnowledgeEmbedder
from agent_core.knowledge.service import KnowledgeService
from tests.integration.m2_support import database_settings

SPACE = EmbeddingSpace(provider="fixture", model="semantic", revision="v1", dimensions=3)
DOCUMENT = (
    "# Guide\n\n## Rollback\n\nRoll back the release by reverting the tag.\n\n"
    "## Fans\n\nThe cooling fan may run for several minutes after shutdown.\n\n"
    "## Notes\n\nNothing about the rollback procedure here, only the procedure for lunch."
)
# Against the question's vector (1, 0, 0): Fans at 1, Rollback at 0.6, Notes
# at 0, below the semantic floor.
VECTORS = {"Rollback": (0.6, 0.8, 0.0), "Fans": (1.0, 0.0, 0.0), "Notes": (0.0, 0.0, 1.0)}


class Provider:
    """Embeds a chunk by the section it belongs to and a question next to the fan passage."""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        vectors = []
        for item in request.texts:
            heading = item.split("\n", 1)[0].rsplit(" > ", 1)[-1]
            vectors.append(VECTORS.get(heading, (1.0, 0.0, 0.0)))
        return EmbeddingResponse(
            request_digest=request.digest, vectors=tuple(vectors), input_tokens=len(vectors)
        )


async def _bytes(value: bytes) -> AsyncIterator[bytes]:
    yield value


def _query(
    principal_id: str, tenant_id: str, text_value: str = "rollback procedure"
) -> KnowledgeQuery:
    return KnowledgeQuery(
        tenant_id=tenant_id,
        principal_id=principal_id,
        current_scope=None,
        text=text_value,
        budget_tokens=3_000,
        max_passages=10,
        max_per_document=3,
        min_score=0.1,
    )


async def test_vectors_are_stored_searched_by_channel_and_cascade(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with build(settings=settings, storage="postgres") as app:
        session_id = await app.sessions.create()
        run_id = uuid4()
        now = app.clock.now()
        async with app.uow_factory() as uow:
            await uow.runs.create(
                Run(
                    id=run_id,
                    session_id=session_id,
                    tenant_id=app.principal.tenant_id,
                    principal_scopes=set(app.principal.scopes),
                    agent_id=DEFAULT_AGENT_ID,
                    agent_version="1.0.0",
                    status=RunStatus.COMPLETED,
                    limits=RunLimits(),
                    scheduled_for=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        store = FilesystemArtifactStore(settings.artifact_root)
        writer = ArtifactWriterFactory(
            app.uow_factory, store, app.clock, RandomIdFactory()
        ).for_run(
            tenant_id=app.principal.tenant_id,
            principal_id=app.principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            origin=ArtifactOrigin.UPLOAD,
        )
        ref = await writer.create(
            _bytes(DOCUMENT.encode()), "guide.md", "text/markdown", TrustLevel.USER
        )
        async with app.uow_factory() as uow:
            source = await uow.artifacts.get(ref.artifact_id, app.principal)

        def service(channel: KnowledgeSemanticChannel) -> KnowledgeService:
            return KnowledgeService(
                app.uow_factory,
                store,
                app.clock,
                RandomIdFactory(),
                app.principal,
                embedder=KnowledgeEmbedder(Provider(), SPACE, channel=channel),
            )

        document = await service("dense").ingest(
            KnowledgeIngestRequest(
                source=source, title="Guide", visibility=KnowledgeVisibility.PRINCIPAL
            ),
            origin_trust=TrustLevel.USER,
        )
        query = _query(app.principal.principal_id, app.principal.tenant_id)
        async with app.uow_factory() as uow:
            lexical = await uow.knowledge.search(query)
            assert [item.heading_path[-1] for item in lexical] == ["Notes", "Rollback"]
            assert all(item.arms == ["lexical"] for item in lexical)
        async with create_engine(settings.database_url).begin() as connection:
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": app.principal.tenant_id},
            )
            stored = await connection.scalar(
                text("SELECT count(*) FROM knowledge_chunk_vectors WHERE space_key = :key"),
                {"key": SPACE.key},
            )
        assert stored == 3

        dense = await service("dense").search(query, session_id=session_id)
        assert [item.heading_path[-1] for item in dense.passages] == ["Fans", "Rollback"]
        assert all(item.arms == ["semantic"] for item in dense.passages)
        hybrid = await service("hybrid").search(query, session_id=session_id)
        arms = {item.heading_path[-1]: item.arms for item in hybrid.passages}
        assert arms["Fans"] == ["semantic"]
        assert arms["Rollback"] == ["lexical", "semantic"]
        assert arms["Notes"] == ["lexical"]

        # A document ingested without the arm has no vectors; the dense
        # channel still reaches it through the lexical arm (ADR-0289).
        older_ref = await writer.create(
            _bytes(b"# Older\n\n## Rollback\n\nThe older rollback procedure."),
            "older.md",
            "text/markdown",
            TrustLevel.USER,
        )
        async with app.uow_factory() as uow:
            older_source = await uow.artifacts.get(older_ref.artifact_id, app.principal)
        older = await app.knowledge.ingest(
            KnowledgeIngestRequest(
                source=older_source, title="Older", visibility=KnowledgeVisibility.PRINCIPAL
            ),
            origin_trust=TrustLevel.USER,
        )
        dense = await service("dense").search(query, session_id=session_id)
        by_document = {item.document_id: item.arms for item in dense.passages}
        assert by_document[older.document_id] == ["lexical"]
        assert by_document[document.document_id] == ["semantic"]

        # Another principal reads nothing through either arm.
        foreign = await service("dense").search(
            _query("another-principal", app.principal.tenant_id), session_id=session_id
        )
        assert foreign.passages == []

        await service("dense").delete(document.document_id)
        async with create_engine(settings.database_url).begin() as connection:
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": app.principal.tenant_id},
            )
            remaining = await connection.scalar(
                text("SELECT count(*) FROM knowledge_chunk_vectors")
            )
        assert remaining == 0
        left = (await service("dense").search(query, session_id=session_id)).passages
        assert {item.document_id for item in left} == {older.document_id}


async def test_ingest_without_an_embedder_stores_no_vectors(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with build(settings=settings, storage="postgres") as app:
        session_id = await app.sessions.create()
        run_id = uuid4()
        now = app.clock.now()
        async with app.uow_factory() as uow:
            await uow.runs.create(
                Run(
                    id=run_id,
                    session_id=session_id,
                    tenant_id=app.principal.tenant_id,
                    principal_scopes=set(app.principal.scopes),
                    agent_id=DEFAULT_AGENT_ID,
                    agent_version="1.0.0",
                    status=RunStatus.COMPLETED,
                    limits=RunLimits(),
                    scheduled_for=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        store = FilesystemArtifactStore(settings.artifact_root)
        writer = ArtifactWriterFactory(
            app.uow_factory, store, app.clock, RandomIdFactory()
        ).for_run(
            tenant_id=app.principal.tenant_id,
            principal_id=app.principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            origin=ArtifactOrigin.UPLOAD,
        )
        ref = await writer.create(
            _bytes(DOCUMENT.encode()), "guide.md", "text/markdown", TrustLevel.USER
        )
        async with app.uow_factory() as uow:
            source = await uow.artifacts.get(ref.artifact_id, app.principal)
        document = await app.knowledge.ingest(
            KnowledgeIngestRequest(
                source=source, title="Guide", visibility=KnowledgeVisibility.PRINCIPAL
            ),
            origin_trust=TrustLevel.USER,
        )
        assert isinstance(document.document_id, UUID)
        async with create_engine(settings.database_url).begin() as connection:
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": app.principal.tenant_id},
            )
            stored = await connection.scalar(text("SELECT count(*) FROM knowledge_chunk_vectors"))
        assert stored == 0


def _words(text: str) -> int:
    return len(text.split())


class Windowed(Provider):
    """Only the window holding "fan" sits by the question; a chunk's other windows do not."""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        vectors = tuple(
            (1.0, 0.0, 0.0)
            if "fan" in item.split("\n", 1)[-1] or item == "rollback procedure"
            else (0.0, 1.0, 0.0)
            for item in request.texts
        )
        return EmbeddingResponse(
            request_digest=request.digest, vectors=vectors, input_tokens=len(vectors)
        )


async def test_windows_are_stored_scored_by_their_best_and_backfilled(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with build(settings=settings, storage="postgres") as app:
        session_id = await app.sessions.create()
        run_id = uuid4()
        now = app.clock.now()
        async with app.uow_factory() as uow:
            await uow.runs.create(
                Run(
                    id=run_id,
                    session_id=session_id,
                    tenant_id=app.principal.tenant_id,
                    principal_scopes=set(app.principal.scopes),
                    agent_id=DEFAULT_AGENT_ID,
                    agent_version="1.0.0",
                    status=RunStatus.COMPLETED,
                    limits=RunLimits(),
                    scheduled_for=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        store = FilesystemArtifactStore(settings.artifact_root)
        writer = ArtifactWriterFactory(
            app.uow_factory, store, app.clock, RandomIdFactory()
        ).for_run(
            tenant_id=app.principal.tenant_id,
            principal_id=app.principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            origin=ArtifactOrigin.UPLOAD,
        )
        # The older document is ingested without the arm, as every document
        # was before activation; the guide is ingested with it, in windows.
        older_ref = await writer.create(
            _bytes(DOCUMENT.encode()), "older.md", "text/markdown", TrustLevel.USER
        )
        async with app.uow_factory() as uow:
            older_source = await uow.artifacts.get(older_ref.artifact_id, app.principal)
        older = await app.knowledge.ingest(
            KnowledgeIngestRequest(
                source=older_source, title="Older", visibility=KnowledgeVisibility.PRINCIPAL
            ),
            origin_trust=TrustLevel.USER,
        )
        embedder = KnowledgeEmbedder(
            Windowed(), SPACE, channel="dense", window_tokens=6, count_tokens=_words
        )
        service = KnowledgeService(
            app.uow_factory, store, app.clock, RandomIdFactory(), app.principal, embedder=embedder
        )
        ref = await writer.create(
            _bytes(DOCUMENT.encode()), "guide.md", "text/markdown", TrustLevel.USER
        )
        async with app.uow_factory() as uow:
            source = await uow.artifacts.get(ref.artifact_id, app.principal)
        guide = await service.ingest(
            KnowledgeIngestRequest(
                source=source, title="Guide", visibility=KnowledgeVisibility.PRINCIPAL
            ),
            origin_trust=TrustLevel.USER,
        )

        async def vector_rows() -> list[tuple[int, int]]:
            async with create_engine(settings.database_url).begin() as connection:
                await connection.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": app.principal.tenant_id},
                )
                rows = await connection.execute(
                    text(
                        "SELECT count(*), count(DISTINCT chunk_id) FROM knowledge_chunk_vectors "
                        "WHERE space_key = :key"
                    ),
                    {"key": SPACE.key},
                )
                return [tuple(row) for row in rows]

        ((windows, chunks),) = await vector_rows()
        assert chunks == 3 and windows > 3, "every section is wider than six words"

        query = _query(app.principal.principal_id, app.principal.tenant_id)
        found = await service.search(query, session_id=session_id)
        by_document = {item.document_id: item for item in found.passages}
        assert by_document[guide.document_id].heading_path[-1] == "Fans"
        assert by_document[guide.document_id].arms == ["semantic"]
        assert by_document[older.document_id].arms == ["lexical"]

        backfill = KnowledgeVectorBackfill(
            uow_factory=app.uow_factory,
            embedder=embedder,
            principal=app.principal,
            clock=app.clock,
            batch_chunks=2,
        )
        assert await backfill.sweep_once() == 2
        assert await backfill.sweep_once() == 1
        assert await backfill.sweep_once() == 0
        ((windows_after, chunks_after),) = await vector_rows()
        assert chunks_after == 6 and windows_after == 2 * windows
        found = await service.search(query, session_id=session_id)
        assert {item.document_id for item in found.passages} == {
            guide.document_id,
            older.document_id,
        }
        assert all(item.arms == ["semantic"] for item in found.passages)
        assert {item.heading_path[-1] for item in found.passages} == {"Fans"}
