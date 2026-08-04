"""Content-addressed local byte store for perishable trajectory exports."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO

from agent_core.domain.trajectory import ArtifactRef


class LocalTrajectoryArtifactStore:
    _CHUNK_BYTES = 64 * 1024

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _relative(self, artifact: ArtifactRef) -> Path:
        tenant_segment = hashlib.sha256(artifact.tenant_id.encode("utf-8")).hexdigest()[:16]
        return Path("trajectory") / tenant_segment / str(artifact.id) / f"{artifact.sha256}.json"

    def _resolve(self, artifact: ArtifactRef) -> Path:
        relative = Path(artifact.storage_uri)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact storage key must be a relative platform key")
        resolved = (self._root / relative).resolve()
        if not resolved.is_relative_to(self._root):
            raise ValueError("artifact storage key escaped its configured root")
        return resolved

    async def write(self, artifact: ArtifactRef, content: bytes) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        if digest != artifact.sha256 or len(content) != artifact.size_bytes:
            raise ValueError("artifact content does not match its declared digest and size")
        stored = artifact.model_copy(update={"storage_uri": self._relative(artifact).as_posix()})
        destination = self._resolve(stored)

        def write_atomic() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".trajectory-", dir=destination.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        await asyncio.to_thread(write_atomic)
        return stored

    async def read(self, artifact: ArtifactRef) -> bytes:
        content = await asyncio.to_thread(self._resolve(artifact).read_bytes)
        if (
            hashlib.sha256(content).hexdigest() != artifact.sha256
            or len(content) != artifact.size_bytes
        ):
            raise ValueError("artifact content digest or size no longer matches its metadata")
        return content

    async def stream(self, artifact: ArtifactRef) -> AsyncIterator[bytes]:
        """Verify without materializing, then yield from that same descriptor."""

        path = self._resolve(artifact)

        def open_verified() -> BinaryIO:
            digest = hashlib.sha256()
            size = 0
            source = path.open("rb")
            try:
                while chunk := source.read(self._CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                if digest.hexdigest() != artifact.sha256 or size != artifact.size_bytes:
                    raise ValueError(
                        "artifact content digest or size no longer matches its metadata"
                    )
                source.seek(0)
                return source
            except BaseException:
                source.close()
                raise

        source = await asyncio.to_thread(open_verified)
        try:
            while chunk := await asyncio.to_thread(source.read, self._CHUNK_BYTES):
                yield chunk
        finally:
            await asyncio.to_thread(source.close)

    async def delete(self, artifact: ArtifactRef) -> None:
        path = self._resolve(artifact)

        def remove() -> None:
            path.unlink(missing_ok=True)
            for parent in (path.parent, path.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    break

        await asyncio.to_thread(remove)
