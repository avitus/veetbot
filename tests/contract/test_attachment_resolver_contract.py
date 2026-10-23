"""AttachmentResolver contract (ADR-0120): only the run's own live uploads."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import FixedClock
from agent_core.application.attachments import StoredAttachmentResolver
from agent_core.bootstrap import build
from agent_core.domain.artifacts import ArtifactMetadata, ArtifactOrigin, AttachmentRead
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from agent_core.domain.views import TextContentBlock
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 9, 23, tzinfo=UTC)


async def _chunks(content: bytes) -> object:
    yield content


async def test_resolver_releases_only_the_runs_live_uploads(tmp_path: Path) -> None:
    from dataclasses import replace

    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")
    async with build(settings=settings, sequential_ids=True) as composition:
        principal = composition.principal
        store = FilesystemArtifactStore(tmp_path / "artifacts")
        session = await composition.services.sessions.create(principal, "general", {})
        other = await composition.services.sessions.create(principal, "general", {})
        submitted = await composition.services.runs.submit(
            principal, session.id, [TextContentBlock(text="hi")], None, None
        )

        async def stored(
            n: int,
            content: bytes,
            *,
            session_id: UUID,
            expires_at: datetime | None,
            origin: str = "upload",
            metadata: dict[str, object] | None = None,
        ) -> UUID:
            artifact_id = UUID(int=n)
            ref = await store.put(
                _chunks(content),  # type: ignore[arg-type]
                ArtifactMetadata(
                    artifact_id=artifact_id,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    session_id=session_id,
                    run_id=None,
                    origin=ArtifactOrigin.UPLOAD,
                    filename="f",
                    media_type="text/plain",
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    trust=TrustLevel.EXTERNAL_UNTRUSTED,
                    created_at=NOW,
                    expires_at=expires_at,
                ),
            )
            async with composition.uow_factory() as uow:
                await uow.artifacts.create(
                    ArtifactRef(
                        id=artifact_id,
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        session_id=session_id,
                        run_id=None if origin == "upload" else submitted.run_id,
                        name="f.txt",
                        media_type="text/plain",
                        storage_uri="",
                        sha256=ref.sha256,
                        size_bytes=ref.size_bytes,
                        origin=origin,  # type: ignore[arg-type]
                        trust=TrustLevel.EXTERNAL_UNTRUSTED,
                        expires_at=expires_at,
                        created_at=NOW,
                        metadata=(
                            {"attachment": {"kind": "text"}} if metadata is None else metadata
                        ),
                    )
                )
            return artifact_id

        later = composition.clock.now() + timedelta(days=1)
        live = await stored(1, b"hello world", session_id=session.id, expires_at=later)
        long = await stored(2, b"x" * 100, session_id=session.id, expires_at=None)
        elsewhere = await stored(3, b"other", session_id=other.id, expires_at=later)
        expired = await stored(4, b"old", session_id=session.id, expires_at=composition.clock.now())
        tool = await stored(
            5,
            b"tool",
            session_id=session.id,
            expires_at=later,
            origin="tool_output",
            metadata={},
        )
        resolver = StoredAttachmentResolver(
            uow_factory=composition.uow_factory,
            store=store,
            principal=principal,
            clock=FixedClock(composition.clock.now()),
        )
        reads = [
            AttachmentRead(artifact_id=live, max_bytes=1_000),
            AttachmentRead(artifact_id=long, max_bytes=10),
            AttachmentRead(artifact_id=elsewhere, max_bytes=1_000),
            AttachmentRead(artifact_id=expired, max_bytes=1_000),
            AttachmentRead(artifact_id=tool, max_bytes=1_000),
            AttachmentRead(artifact_id=UUID(int=99), max_bytes=1_000),
        ]
        released = await resolver.resolve(reads, run_id=submitted.run_id)
        assert set(released) == {live, long}
        assert released[live].data == b"hello world"
        assert released[live].truncated is False
        assert released[long].data == b"x" * 10
        assert released[long].truncated is True
        assert await resolver.resolve(reads, run_id=UUID(int=12345)) == {}
        assert await resolver.resolve([], run_id=submitted.run_id) == {}


async def test_resolver_reports_only_the_runs_documents_sent_to_knowledge() -> None:
    async with build(settings=memory_settings(), sequential_ids=True) as composition:
        principal = composition.principal
        session = await composition.services.sessions.create(principal, "general", {})
        other = await composition.services.sessions.create(principal, "general", {})
        submitted = await composition.services.runs.submit(
            principal, session.id, [TextContentBlock(text="hi")], None, None
        )

        async def stored(
            n: int, state: str | None, *, session_id: UUID, origin: str = "upload"
        ) -> UUID:
            metadata: dict[str, object] = {"attachment": {"kind": "pdf", "page_count": 424}}
            if state is not None:
                metadata["auto_ingest"] = state
            async with composition.uow_factory() as uow:
                await uow.artifacts.create(
                    ArtifactRef(
                        id=UUID(int=n),
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        session_id=session_id,
                        run_id=submitted.run_id,
                        name="manual.pdf",
                        media_type="application/pdf",
                        storage_uri="",
                        sha256="0" * 64,
                        size_bytes=5_764_203,
                        origin=origin,  # type: ignore[arg-type]
                        trust=TrustLevel.EXTERNAL_UNTRUSTED,
                        expires_at=None,
                        created_at=NOW,
                        metadata=metadata,
                    )
                )
            return UUID(int=n)

        pending = await stored(1, "pending", session_id=session.id)
        ingested = await stored(2, "ingested", session_id=session.id, origin="knowledge_source")
        refused = await stored(3, "refused", session_id=session.id, origin="knowledge_source")
        failed = await stored(4, "failed", session_id=session.id)
        unmarked = await stored(5, None, session_id=session.id)
        elsewhere = await stored(6, "pending", session_id=other.id)
        resolver = StoredAttachmentResolver(
            uow_factory=composition.uow_factory,
            store=FilesystemArtifactStore(Path("unused")),
            principal=principal,
            clock=FixedClock(composition.clock.now()),
        )
        asked = [pending, ingested, refused, failed, unmarked, elsewhere, UUID(int=99)]
        assert await resolver.knowledge_bound(asked, run_id=submitted.run_id) == {
            pending,
            ingested,
        }
        assert await resolver.knowledge_bound(asked, run_id=UUID(int=12345)) == frozenset()
        assert await resolver.knowledge_bound([], run_id=submitted.run_id) == frozenset()
