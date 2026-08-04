"""Narrow execution-service ports; intentionally not a filesystem API."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Protocol

from agent_core.domain.execution import WorkspaceEntry, WorkspaceProvenance


class WorkspaceHandle(Protocol):
    @property
    def root(self) -> PurePosixPath: ...

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath: ...

    async def read(self, path: str) -> bytes: ...

    async def write(self, path: str, data: bytes) -> None: ...

    async def listdir(self, path: str, *, recursive: bool = False) -> Sequence[WorkspaceEntry]: ...

    async def provenance(self, path: str) -> WorkspaceProvenance: ...


class WorkspaceFactory(Protocol):
    def for_run(self, tenant_id: str, run_id: object) -> WorkspaceHandle: ...
