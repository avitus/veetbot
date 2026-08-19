"""Artifact writer providers bind platform identity before a tool writes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.errors import NotFoundError
from agent_core.domain.policies import TrustLevel
from tests.contract.support import RUN_ID, SESSION_ID, TENANT, memory_uow_factory, principal


async def _one_chunk() -> AsyncIterator[bytes]:
    yield b"sandbox export bytes"


async def test_for_run_binds_platform_identity_onto_the_created_artifact(
    tmp_path: Path,
) -> None:
    clock, factory = await memory_uow_factory()
    artifact_id = UUID(int=8810)
    provider = ArtifactWriterFactory(
        factory,
        FilesystemArtifactStore(tmp_path),
        clock,
        SequenceIdFactory([artifact_id]),
    )

    writer = provider.for_run(
        tenant_id=TENANT,
        principal_id=principal().principal_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        origin=ArtifactOrigin.SANDBOX_EXPORT,
    )
    stored = await writer.create(
        _one_chunk(), "export.bin", "application/octet-stream", TrustLevel.EXTERNAL_UNTRUSTED
    )

    assert stored.artifact_id == artifact_id
    async with factory() as uow:
        row = await uow.artifacts.get(artifact_id, principal())
        with pytest.raises(NotFoundError):
            await uow.artifacts.get(
                artifact_id,
                principal().model_copy(update={"tenant_id": "tenant-b"}),
            )
    assert row.tenant_id == TENANT
    assert row.principal_id == principal().principal_id
    assert row.session_id == SESSION_ID
    assert row.run_id == RUN_ID
    assert row.origin == ArtifactOrigin.SANDBOX_EXPORT.value
    assert row.trust is TrustLevel.EXTERNAL_UNTRUSTED
