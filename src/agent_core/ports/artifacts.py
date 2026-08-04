"""Narrow Milestone 3 byte store for governed trajectory exports."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from agent_core.domain.artifacts import ArtifactMetadata, ArtifactOrigin, StoredArtifactRef
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef


class TrajectoryArtifactStore(Protocol):
    async def write(self, artifact: ArtifactRef, content: bytes) -> ArtifactRef: ...

    async def read(self, artifact: ArtifactRef) -> bytes: ...

    def stream(self, artifact: ArtifactRef) -> AsyncIterator[bytes]: ...

    async def delete(self, artifact: ArtifactRef) -> None: ...


class ArtifactStore(Protocol):
    async def put(
        self,
        stream: AsyncIterator[bytes],
        metadata: ArtifactMetadata,
    ) -> StoredArtifactRef: ...

    def open(self, ref: StoredArtifactRef, *, tenant_id: str) -> AsyncIterator[bytes]: ...

    async def delete(self, ref: StoredArtifactRef, *, tenant_id: str) -> None: ...


class ArtifactWriter(Protocol):
    async def create(
        self,
        stream: AsyncIterator[bytes],
        filename: str,
        media_type: str,
        trust: TrustLevel,
    ) -> StoredArtifactRef: ...


class ArtifactWriterProvider(Protocol):
    def for_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        session_id: UUID,
        run_id: UUID,
        origin: ArtifactOrigin,
    ) -> ArtifactWriter: ...
