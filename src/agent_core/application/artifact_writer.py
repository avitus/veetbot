"""Run-bound artifact capability supplied to tools by the pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from agent_core.domain.artifacts import ArtifactMetadata, ArtifactOrigin, StoredArtifactRef
from agent_core.domain.errors import ArtifactIntegrityError
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.artifacts import ArtifactStore, ArtifactWriter
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory

logger = logging.getLogger(__name__)


class _ReadableBytes(Protocol):
    def read(self, size: int = -1) -> bytes: ...


async def _file_stream(
    source: _ReadableBytes, chunk_bytes: int = 64 * 1024
) -> AsyncIterator[bytes]:
    while chunk := await asyncio.to_thread(source.read, chunk_bytes):
        yield chunk


class BoundArtifactWriter:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        store: ArtifactStore,
        clock: Clock,
        ids: IdFactory,
        tenant_id: str,
        principal_id: str,
        session_id: UUID,
        run_id: UUID,
        origin: ArtifactOrigin,
        retention_days: int = 30,
        maximum_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self._uow_factory = uow_factory
        self._store = store
        self._clock = clock
        self._ids = ids
        self._tenant_id = tenant_id
        self._principal_id = principal_id
        self._session_id = session_id
        self._run_id = run_id
        self._origin = origin
        self._retention_days = retention_days
        self._maximum_bytes = maximum_bytes

    async def create(
        self,
        stream: AsyncIterator[bytes],
        filename: str,
        media_type: str,
        trust: TrustLevel,
    ) -> StoredArtifactRef:
        digest = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
            async for chunk in stream:
                digest.update(chunk)
                size += len(chunk)
                if size > self._maximum_bytes:
                    raise ArtifactIntegrityError("artifact exceeds the configured size cap")
                await asyncio.to_thread(spool.write, chunk)
            await asyncio.to_thread(spool.seek, 0)
            artifact_id = self._ids.new_id()
            now = self._clock.now()
            expires_at = now + timedelta(days=self._retention_days)
            metadata = ArtifactMetadata(
                artifact_id=artifact_id,
                tenant_id=self._tenant_id,
                principal_id=self._principal_id,
                session_id=self._session_id,
                run_id=self._run_id,
                origin=self._origin,
                filename=filename,
                media_type=media_type,
                size_bytes=size,
                sha256=digest.hexdigest(),
                trust=trust,
                created_at=now,
                expires_at=expires_at,
            )
            stored = await self._store.put(_file_stream(spool), metadata)
        artifact = ArtifactRef(
            id=artifact_id,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            session_id=self._session_id,
            run_id=self._run_id,
            name=filename,
            media_type=media_type,
            storage_uri="",
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            origin=self._origin.value,
            trust=trust,
            expires_at=expires_at,
            created_at=now,
        )
        try:
            async with self._uow_factory() as uow:
                await uow.artifacts.create(artifact)
        except BaseException:
            try:
                await self._store.delete(stored, tenant_id=self._tenant_id)
            except BaseException:
                logger.exception(
                    "artifact_rollback_delete_failed",
                    extra={"artifact_id": str(artifact_id)},
                )
            raise
        return stored


class ArtifactWriterFactory:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        store: ArtifactStore,
        clock: Clock,
        ids: IdFactory,
        *,
        retention_days: int = 30,
        maximum_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self._uow_factory = uow_factory
        self._store = store
        self._clock = clock
        self._ids = ids
        self._retention_days = retention_days
        self._maximum_bytes = maximum_bytes

    def for_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        session_id: UUID,
        run_id: UUID,
        origin: ArtifactOrigin,
    ) -> ArtifactWriter:
        return BoundArtifactWriter(
            uow_factory=self._uow_factory,
            store=self._store,
            clock=self._clock,
            ids=self._ids,
            tenant_id=tenant_id,
            principal_id=principal_id,
            session_id=session_id,
            run_id=run_id,
            origin=origin,
            retention_days=self._retention_days,
            maximum_bytes=self._maximum_bytes,
        )

    async def sweep_expired(self, *, limit: int = 100) -> int:
        """Delete expired general-artifact bytes before their metadata rows."""

        now = self._clock.now()
        async with self._uow_factory() as uow:
            expired = await uow.artifacts.list_expired(now, limit=limit)
        removed = 0
        failures: list[Exception] = []
        for artifact in expired:
            ref = StoredArtifactRef(
                artifact_id=artifact.id,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                media_type=artifact.media_type,
            )
            try:
                await self._store.delete(ref, tenant_id=artifact.tenant_id)
                async with self._uow_factory() as uow:
                    removed += int(await uow.artifacts.delete_expired(artifact.id, now=now))
            # Aggregate all candidate failures so one object cannot stop the sweep.
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("one or more expired artifact deletions failed", failures)
        return removed
