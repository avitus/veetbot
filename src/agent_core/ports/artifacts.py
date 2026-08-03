"""Narrow Milestone 3 byte store for governed trajectory exports."""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.trajectory import ArtifactRef


class TrajectoryArtifactStore(Protocol):
    async def write(self, artifact: ArtifactRef, content: bytes) -> ArtifactRef: ...

    async def read(self, artifact: ArtifactRef) -> bytes: ...

    async def delete(self, artifact: ArtifactRef) -> None: ...
