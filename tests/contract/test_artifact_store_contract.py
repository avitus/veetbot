"""Streaming artifact-store integrity contract."""

import hashlib
from collections.abc import AsyncIterator
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
    ref = await store.put(_chunks(content), _metadata(content))
    assert b"".join([chunk async for chunk in store.open(ref, tenant_id="tenant-a")]) == content
    assert "report" not in str(next(tmp_path.rglob(str(ref.artifact_id))))


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
