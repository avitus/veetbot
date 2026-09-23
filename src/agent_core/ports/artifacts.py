"""Narrow Milestone 3 byte store for governed trajectory exports."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol
from uuid import UUID

from agent_core.domain.artifacts import (
    ArtifactMetadata,
    ArtifactOrigin,
    AttachmentContent,
    AttachmentFacts,
    AttachmentRead,
    StoredArtifactRef,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef


class TrajectoryArtifactStore(Protocol):
    async def write(self, artifact: ArtifactRef, content: bytes) -> ArtifactRef: ...

    async def read(self, artifact: ArtifactRef) -> bytes: ...

    def stream(self, artifact: ArtifactRef) -> AsyncIterator[bytes]: ...

    async def open_verified(self, artifact: ArtifactRef) -> AsyncIterator[bytes]: ...

    async def delete(self, artifact: ArtifactRef) -> None: ...


class ArtifactStore(Protocol):
    async def put(
        self,
        stream: AsyncIterator[bytes],
        metadata: ArtifactMetadata,
    ) -> StoredArtifactRef: ...

    def open(self, ref: StoredArtifactRef, *, tenant_id: str) -> AsyncIterator[bytes]: ...

    async def open_verified(
        self, ref: StoredArtifactRef, *, tenant_id: str
    ) -> AsyncIterator[bytes]: ...

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


class AttachmentInspector(Protocol):
    """Classify an upload from its bytes (ADR-0118).

    The result's media type is the detected one; a declared type is kept only
    when nothing in the bytes contradicts it. Inspection never refuses a file:
    anything it cannot read is kind `other`.
    """

    async def inspect(
        self, content: bytes, *, filename: str, declared_media_type: str
    ) -> AttachmentFacts: ...


class AttachmentResolver(Protocol):
    """Release attachment bytes to a model adapter for one run (ADR-0118).

    Only an upload of the run's principal in the run's session that has not
    expired is released; anything else is absent from the result, and the
    adapter renders it as a marker. Reads never exceed their `max_bytes`.
    """

    async def resolve(
        self, reads: Sequence[AttachmentRead], *, run_id: UUID
    ) -> Mapping[UUID, AttachmentContent]: ...
