"""Streaming filesystem artifact store with derived opaque keys."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from agent_core.domain.artifacts import ArtifactMetadata, StoredArtifactRef
from agent_core.domain.errors import ArtifactIntegrityError


def artifact_storage_key(tenant_id: str, artifact_id: UUID) -> Path:
    """Derive a key only from platform identity; filenames never participate."""

    tenant_segment = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    artifact_segment = str(artifact_id)
    return Path("objects") / tenant_segment / artifact_segment[:2] / artifact_segment


class FilesystemArtifactStore:
    _CHUNK_BYTES = 64 * 1024

    def __init__(self, root: Path, *, maximum_bytes: int = 512 * 1024 * 1024) -> None:
        self._root = root.resolve()
        self._maximum_bytes = maximum_bytes

    def _path(self, tenant_id: str, artifact_id: UUID) -> Path:
        destination = (self._root / artifact_storage_key(tenant_id, artifact_id)).resolve()
        if not destination.is_relative_to(self._root):
            raise ArtifactIntegrityError("artifact key escaped its configured root")
        return destination

    async def put(
        self,
        stream: AsyncIterator[bytes],
        metadata: ArtifactMetadata,
    ) -> StoredArtifactRef:
        destination = self._path(metadata.tenant_id, metadata.artifact_id)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=destination.parent)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as target:
                async for chunk in stream:
                    if not isinstance(chunk, bytes):
                        raise TypeError("artifact stream must yield bytes")
                    size += len(chunk)
                    if size > self._maximum_bytes:
                        raise ArtifactIntegrityError("artifact exceeds the configured size cap")
                    digest.update(chunk)
                    await asyncio.to_thread(target.write, chunk)
                await asyncio.to_thread(target.flush)
                await asyncio.to_thread(os.fsync, target.fileno())
            if size != metadata.size_bytes or digest.hexdigest() != metadata.sha256:
                raise ArtifactIntegrityError("artifact stream does not match declared metadata")
            await asyncio.to_thread(os.replace, temporary_name, destination)
        except BaseException:
            await asyncio.to_thread(Path(temporary_name).unlink, missing_ok=True)
            raise
        return StoredArtifactRef(
            artifact_id=metadata.artifact_id,
            sha256=metadata.sha256,
            size_bytes=metadata.size_bytes,
            media_type=metadata.media_type,
        )

    async def _open_verified(self, ref: StoredArtifactRef, tenant_id: str) -> BinaryIO:
        path = self._path(tenant_id, ref.artifact_id)

        def open_verified() -> BinaryIO:
            source = path.open("rb")
            digest = hashlib.sha256()
            size = 0
            try:
                while chunk := source.read(self._CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                if digest.hexdigest() != ref.sha256 or size != ref.size_bytes:
                    raise ArtifactIntegrityError("stored artifact no longer matches metadata")
                source.seek(0)
                return source
            except BaseException:
                source.close()
                raise

        return await asyncio.to_thread(open_verified)

    async def open(self, ref: StoredArtifactRef, *, tenant_id: str) -> AsyncIterator[bytes]:
        source = await self._open_verified(ref, tenant_id)
        try:
            while chunk := await asyncio.to_thread(source.read, self._CHUNK_BYTES):
                yield chunk
        finally:
            await asyncio.to_thread(source.close)

    async def delete(self, ref: StoredArtifactRef, *, tenant_id: str) -> None:
        await asyncio.to_thread(self._path(tenant_id, ref.artifact_id).unlink, missing_ok=True)

    async def reconcile_orphans(
        self,
        metadata_exists: Callable[[UUID], Awaitable[bool]],
        *,
        now: datetime,
        safety_margin: timedelta = timedelta(hours=1),
    ) -> int:
        """Delete old committed objects that have no corresponding metadata row."""

        object_root = self._root / "objects"
        paths = await asyncio.to_thread(
            lambda: tuple(path for path in object_root.glob("*/*/*") if path.is_file())
        )
        removed = 0
        threshold = now.timestamp() - safety_margin.total_seconds()
        for claim in (path for path in paths if path.name.startswith(".reconcile-")):
            try:
                artifact_id = UUID(claim.name.removeprefix(".reconcile-")[:36])
                modified = (await asyncio.to_thread(claim.stat)).st_mtime
            except (ValueError, FileNotFoundError):
                continue
            if modified > threshold:
                continue
            destination = claim.with_name(str(artifact_id))
            if await metadata_exists(artifact_id):
                await asyncio.to_thread(_restore_claim, claim, destination)
            else:
                await asyncio.to_thread(claim.unlink, missing_ok=True)
                removed += 1
        for path in paths:
            if path.name.startswith(".reconcile-"):
                continue
            try:
                artifact_id = UUID(path.name)
                metadata = await asyncio.to_thread(path.stat)
            except (ValueError, FileNotFoundError):
                continue
            if metadata.st_mtime > threshold:
                continue
            claim = path.with_name(f".reconcile-{artifact_id}-{metadata.st_ino}")
            try:
                await asyncio.to_thread(os.replace, path, claim)
                claimed = await asyncio.to_thread(claim.stat)
            except FileNotFoundError:
                continue
            if claimed.st_mtime > threshold or await metadata_exists(artifact_id):
                await asyncio.to_thread(_restore_claim, claim, path)
                continue
            await asyncio.to_thread(claim.unlink, missing_ok=True)
            removed += 1
        return removed


def _restore_claim(claim: Path, destination: Path) -> None:
    with suppress(FileExistsError):
        os.link(claim, destination)
    claim.unlink(missing_ok=True)
