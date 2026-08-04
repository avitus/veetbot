"""Streaming artifact-store integrity contract."""

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.domain.artifacts import ArtifactMetadata, ArtifactOrigin, StoredArtifactRef
from agent_core.domain.errors import ArtifactIntegrityError
from agent_core.domain.policies import TrustLevel


async def _chunks(content: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(content), 997):
        yield content[offset : offset + 997]


def _metadata(content: bytes) -> ArtifactMetadata:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return ArtifactMetadata(
        artifact_id=UUID(int=50),
        tenant_id="tenant-a",
        principal_id="user-a",
        session_id=UUID(int=51),
        run_id=UUID(int=52),
        origin=ArtifactOrigin.SANDBOX_EXPORT,
        filename="../../report.bin",
        media_type="application/octet-stream",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        created_at=now,
        expires_at=now + timedelta(days=30),
    )


async def test_artifact_store_round_trip_is_streaming_and_filename_opaque(tmp_path: Path) -> None:
    content = b"artifact" * 20_000
    store = FilesystemArtifactStore(tmp_path)
    escape_name = f"../../{tmp_path.name}-artifact-escape.bin"
    escape_target = (tmp_path / escape_name).resolve()
    metadata = replace(_metadata(content), filename=escape_name)
    assert escape_target.exists() is False
    ref = await store.put(_chunks(content), metadata)
    assert b"".join([chunk async for chunk in store.open(ref, tenant_id="tenant-a")]) == content
    with pytest.raises(FileNotFoundError):
        _ = [chunk async for chunk in store.open(ref, tenant_id="tenant-b")]
    await store.delete(ref, tenant_id="tenant-b")
    assert b"".join([chunk async for chunk in store.open(ref, tenant_id="tenant-a")]) == content
    generated_paths = tuple(path for path in tmp_path.rglob("*") if path.is_file())
    assert generated_paths
    assert all(tmp_path in path.parents for path in generated_paths)
    assert all("artifact-escape" not in str(path) for path in generated_paths)
    assert escape_target.exists() is False


async def test_artifact_store_rejects_digest_drift_without_committing(tmp_path: Path) -> None:
    content = b"bytes"
    store = FilesystemArtifactStore(tmp_path)
    metadata = _metadata(content)
    broken = ArtifactMetadata(
        metadata.artifact_id,
        metadata.tenant_id,
        metadata.principal_id,
        metadata.session_id,
        metadata.run_id,
        metadata.origin,
        metadata.filename,
        metadata.media_type,
        metadata.size_bytes,
        "0" * 64,
        metadata.trust,
        metadata.created_at,
        metadata.expires_at,
    )
    with pytest.raises(ArtifactIntegrityError):
        await store.put(_chunks(content), broken)
    ref = StoredArtifactRef(broken.artifact_id, broken.sha256, broken.size_bytes, broken.media_type)
    with pytest.raises(FileNotFoundError):
        _ = [chunk async for chunk in store.open(ref, tenant_id=broken.tenant_id)]


async def _false(_artifact_id: UUID) -> bool:
    return False


async def test_artifact_store_reconciles_only_old_metadata_orphans(tmp_path: Path) -> None:
    content = b"orphan"
    store = FilesystemArtifactStore(tmp_path)
    orphan = await store.put(_chunks(content), _metadata(content))
    orphan_path = next(tmp_path.rglob(str(orphan.artifact_id)))
    now = datetime(2026, 1, 2, tzinfo=UTC)
    old_timestamp = (now - timedelta(hours=2)).timestamp()
    os.utime(orphan_path, (old_timestamp, old_timestamp))

    assert await store.reconcile_orphans(_false, now=now) == 1
    assert not orphan_path.exists()


async def test_reconciliation_does_not_delete_a_concurrent_replacement(tmp_path: Path) -> None:
    content = b"orphan"
    replacement = b"replacement"
    store = FilesystemArtifactStore(tmp_path)
    metadata = _metadata(content)
    orphan = await store.put(_chunks(content), metadata)
    orphan_path = next(tmp_path.rglob(str(orphan.artifact_id)))
    now = datetime.now(UTC)
    old_timestamp = (now - timedelta(hours=2)).timestamp()
    os.utime(orphan_path, (old_timestamp, old_timestamp))
    replacement_metadata = ArtifactMetadata(
        metadata.artifact_id,
        metadata.tenant_id,
        metadata.principal_id,
        metadata.session_id,
        metadata.run_id,
        metadata.origin,
        metadata.filename,
        metadata.media_type,
        len(replacement),
        hashlib.sha256(replacement).hexdigest(),
        metadata.trust,
        metadata.created_at,
        metadata.expires_at,
    )

    async def replace_during_lookup(_artifact_id: UUID) -> bool:
        await store.put(_chunks(replacement), replacement_metadata)
        return False

    assert await store.reconcile_orphans(replace_during_lookup, now=now) == 1
    ref = StoredArtifactRef(
        replacement_metadata.artifact_id,
        replacement_metadata.sha256,
        replacement_metadata.size_bytes,
        replacement_metadata.media_type,
    )
    assert b"".join([chunk async for chunk in store.open(ref, tenant_id="tenant-a")]) == replacement
