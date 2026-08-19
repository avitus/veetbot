"""Artifact writer contract: bounded capture, durable metadata, and rollback."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.application.artifact_writer import BoundArtifactWriter
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.errors import ArtifactIntegrityError, ConflictError
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from tests.contract.support import (
    NOW,
    RUN_ID,
    SESSION_ID,
    TENANT,
    memory_uow_factory,
    principal,
)


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _writer_stack(
    tmp_path: Path,
    *,
    ids: SequenceIdFactory,
    maximum_bytes: int = 512 * 1024 * 1024,
) -> tuple[BoundArtifactWriter, FilesystemArtifactStore, MemoryUnitOfWorkFactory]:
    clock, factory = await memory_uow_factory()
    store = FilesystemArtifactStore(tmp_path)
    writer = BoundArtifactWriter(
        uow_factory=factory,
        store=store,
        clock=clock,
        ids=ids,
        tenant_id=TENANT,
        principal_id=principal().principal_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        origin=ArtifactOrigin.TOOL_OUTPUT,
        maximum_bytes=maximum_bytes,
    )
    return writer, store, factory


async def test_create_streams_bytes_and_persists_checksummed_metadata(tmp_path: Path) -> None:
    content = b"tool output " * 4_000
    artifact_id = UUID(int=8801)
    writer, store, factory = await _writer_stack(tmp_path, ids=SequenceIdFactory([artifact_id]))

    stored = await writer.create(
        _chunks(content[:5], content[5:]),
        "web-search-output.json",
        "application/json",
        TrustLevel.EXTERNAL_UNTRUSTED,
    )

    assert stored.artifact_id == artifact_id
    assert stored.size_bytes == len(content)
    assert stored.sha256 == hashlib.sha256(content).hexdigest()
    replayed = b"".join(
        [chunk async for chunk in await store.open_verified(stored, tenant_id=TENANT)]
    )
    assert replayed == content
    async with factory() as uow:
        row = await uow.artifacts.get(artifact_id, principal())
    assert row.tenant_id == TENANT
    assert row.session_id == SESSION_ID
    assert row.run_id == RUN_ID
    assert row.origin == ArtifactOrigin.TOOL_OUTPUT.value
    assert row.trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert row.sha256 == stored.sha256
    assert row.expires_at == NOW + timedelta(days=30)


async def test_create_refuses_streams_over_the_size_cap_without_committing(
    tmp_path: Path,
) -> None:
    artifact_id = UUID(int=8802)
    writer, _store, factory = await _writer_stack(
        tmp_path, ids=SequenceIdFactory([artifact_id]), maximum_bytes=8
    )

    with pytest.raises(ArtifactIntegrityError):
        await writer.create(
            _chunks(b"12345", b"6789"),
            "oversized.bin",
            "application/octet-stream",
            TrustLevel.PLATFORM,
        )

    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    async with factory() as uow:
        assert await uow.artifacts.exists(artifact_id) is False


async def test_metadata_conflict_rolls_back_the_stored_bytes(tmp_path: Path) -> None:
    artifact_id = UUID(int=8803)
    writer, _store, factory = await _writer_stack(tmp_path, ids=SequenceIdFactory([artifact_id]))
    existing = ArtifactRef(
        id=artifact_id,
        tenant_id=TENANT,
        principal_id=principal().principal_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        name="already-recorded.txt",
        media_type="text/plain",
        storage_uri="",
        sha256=hashlib.sha256(b"original").hexdigest(),
        size_bytes=len(b"original"),
        origin=ArtifactOrigin.TOOL_OUTPUT.value,
        trust=TrustLevel.PLATFORM,
        expires_at=NOW + timedelta(days=30),
        created_at=NOW,
    )
    async with factory() as uow:
        await uow.artifacts.create(existing)

    with pytest.raises(ConflictError):
        await writer.create(
            _chunks(b"replacement"), "second.txt", "text/plain", TrustLevel.PLATFORM
        )

    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    async with factory() as uow:
        row = await uow.artifacts.get(artifact_id, principal())
    assert row.name == "already-recorded.txt"
