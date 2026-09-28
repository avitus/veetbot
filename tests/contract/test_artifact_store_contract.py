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
    broken = replace(metadata, sha256="0" * 64)
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
    replacement_metadata = replace(
        metadata,
        size_bytes=len(replacement),
        sha256=hashlib.sha256(replacement).hexdigest(),
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


async def test_reconciliation_tolerates_a_claim_removed_by_another_sweep(
    tmp_path: Path,
) -> None:
    content = b"concurrent orphan"
    store = FilesystemArtifactStore(tmp_path)
    orphan = await store.put(_chunks(content), _metadata(content))
    orphan_path = next(tmp_path.rglob(str(orphan.artifact_id)))
    now = datetime(2026, 1, 2, tzinfo=UTC)
    old_timestamp = (now - timedelta(hours=2)).timestamp()
    os.utime(orphan_path, (old_timestamp, old_timestamp))

    async def remove_claim(_artifact_id: UUID) -> bool:
        claim = next(tmp_path.rglob(f".reconcile-{orphan.artifact_id}-*"))
        claim.unlink()
        return True

    assert await store.reconcile_orphans(remove_claim, now=now) == 0


def _ref(metadata: ArtifactMetadata) -> StoredArtifactRef:
    return StoredArtifactRef(
        metadata.artifact_id, metadata.sha256, metadata.size_bytes, metadata.media_type
    )


def _age(path: Path, now: datetime, *, hours: float) -> None:
    timestamp = (now - timedelta(hours=hours)).timestamp()
    os.utime(path, (timestamp, timestamp))


async def test_reads_refuse_bytes_that_changed_after_commit(tmp_path: Path) -> None:
    content = b"committed bytes"
    store = FilesystemArtifactStore(tmp_path)
    ref = await store.put(_chunks(content), _metadata(content))
    stored = next(tmp_path.rglob(str(ref.artifact_id)))
    stored.write_bytes(b"tampered bytes!")

    with pytest.raises(ArtifactIntegrityError):
        _ = [chunk async for chunk in store.open(ref, tenant_id="tenant-a")]
    with pytest.raises(ArtifactIntegrityError):
        await store.open_verified(ref, tenant_id="tenant-a")


async def test_the_store_size_cap_leaves_no_partial_object(tmp_path: Path) -> None:
    content = b"12345"
    store = FilesystemArtifactStore(tmp_path, maximum_bytes=4)

    with pytest.raises(ArtifactIntegrityError, match="size cap"):
        await store.put(_chunks(content), _metadata(content))

    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


async def test_reconciliation_keeps_recorded_and_recent_objects(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    recorded = replace(_metadata(b"recorded"), artifact_id=UUID(int=60))
    recent = replace(_metadata(b"recent"), artifact_id=UUID(int=61))
    await store.put(_chunks(b"recorded"), recorded)
    await store.put(_chunks(b"recent"), recent)
    now = datetime.now(UTC)
    _age(next(tmp_path.rglob(str(recorded.artifact_id))), now, hours=2)
    looked_up: list[UUID] = []

    async def exists(artifact_id: UUID) -> bool:
        looked_up.append(artifact_id)
        return artifact_id == recorded.artifact_id

    assert await store.reconcile_orphans(exists, now=now) == 0

    assert looked_up == [recorded.artifact_id]
    for metadata, content in ((recorded, b"recorded"), (recent, b"recent")):
        chunks = [chunk async for chunk in store.open(_ref(metadata), tenant_id="tenant-a")]
        assert b"".join(chunks) == content
    assert not list(tmp_path.rglob(".reconcile-*"))


async def test_reconciliation_resolves_claims_left_by_an_interrupted_sweep(
    tmp_path: Path,
) -> None:
    """A sweep that crashed after claiming an object leaves a ``.reconcile-`` name.
    The next sweep restores a claimed object that has metadata, removes one that
    has none, and leaves a claim young enough to belong to a live sweep alone."""

    store = FilesystemArtifactStore(tmp_path)
    now = datetime.now(UTC)
    claims = {}
    for number, content in ((70, b"recorded"), (71, b"orphaned"), (72, b"in flight")):
        metadata = replace(_metadata(content), artifact_id=UUID(int=number))
        await store.put(_chunks(content), metadata)
        committed = next(tmp_path.rglob(str(metadata.artifact_id)))
        claim = committed.with_name(f".reconcile-{metadata.artifact_id}-{number}")
        committed.rename(claim)
        claims[number] = (metadata, committed, claim)
    _age(claims[70][2], now, hours=2)
    _age(claims[71][2], now, hours=2)

    async def exists(artifact_id: UUID) -> bool:
        return artifact_id == UUID(int=70)

    assert await store.reconcile_orphans(exists, now=now) == 1

    recorded, recorded_path, recorded_claim = claims[70]
    assert recorded_path.exists() and not recorded_claim.exists()
    chunks = [chunk async for chunk in store.open(_ref(recorded), tenant_id="tenant-a")]
    assert b"".join(chunks) == b"recorded"
    _orphan, orphan_path, orphan_claim = claims[71]
    assert not orphan_path.exists() and not orphan_claim.exists()
    _live, live_path, live_claim = claims[72]
    assert live_claim.exists() and not live_path.exists()
