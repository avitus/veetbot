"""PostgreSQL metadata and filesystem bytes for general run artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import RandomIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import build
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.policies import TrustLevel
from agent_core.domain.views import TextContentBlock
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings


async def _chunks(content: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(content), 8191):
        yield content[offset : offset + 8191]


async def test_postgres_artifact_metadata_and_content_round_trip(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path)
    content = b"durable sandbox artifact\n" * 10_000
    async with build(settings=settings, storage="postgres") as composition:
        session = await composition.services.sessions.create(composition.principal, "general", {})
        submitted = await composition.services.runs.submit(
            composition.principal,
            session.id,
            [TextContentBlock(text="create a run for an artifact")],
            None,
            None,
        )
        run_id = submitted.run_id
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="m6-artifact-worker",
        )
        assert await worker.run_once()
        writer = ArtifactWriterFactory(
            composition.uow_factory,
            FilesystemArtifactStore(tmp_path),
            composition.clock,
            RandomIdFactory(),
        ).for_run(
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=session.id,
            run_id=run_id,
            origin=ArtifactOrigin.SANDBOX_EXPORT,
        )
        ref = await writer.create(
            _chunks(content),
            "result.bin",
            "application/octet-stream",
            TrustLevel.EXTERNAL_UNTRUSTED,
        )
        view = await composition.services.artifacts.get(composition.principal, ref.artifact_id)
        opened = await composition.services.artifacts.open_content(
            composition.principal, ref.artifact_id
        )
        downloaded = b"".join([chunk async for chunk in await opened.open()])
        expiry = ArtifactWriterFactory(
            composition.uow_factory,
            FilesystemArtifactStore(tmp_path),
            composition.clock,
            RandomIdFactory(),
            retention_days=0,
        )
        expired_ref = await expiry.for_run(
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=session.id,
            run_id=run_id,
            origin=ArtifactOrigin.TOOL_OUTPUT,
        ).create(
            _chunks(b"expired"),
            "expired.bin",
            "application/octet-stream",
            TrustLevel.INTERNAL_TOOL,
        )
        assert await expiry.sweep_expired() == 1
        assert await expiry.sweep_expired() == 0
        async with composition.uow_factory() as uow:
            assert await uow.artifacts.exists(expired_ref.artifact_id) is False
            assert await uow.artifacts.exists(ref.artifact_id) is True

    assert view.sha256 == hashlib.sha256(content).hexdigest()
    assert view.size_bytes == len(content)
    assert downloaded == content
