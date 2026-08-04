"""Narrow execution-service ports; intentionally not a filesystem API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import PurePosixPath
from typing import Protocol

from agent_core.domain.execution import (
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    WorkspaceEntry,
    WorkspaceProvenance,
)


class ExecutionEnvironment(Protocol):
    async def provision(self, specification: EnvironmentSpec) -> EnvironmentHandle: ...

    async def execute(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
    ) -> ExecutionResult: ...

    async def destroy(self, environment: EnvironmentHandle) -> None: ...


class WorkspaceHandle(Protocol):
    @property
    def root(self) -> PurePosixPath: ...

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath: ...

    async def read(self, path: str) -> bytes: ...

    async def read_bounded(self, path: str, maximum_bytes: int) -> bytes: ...

    def stream(self, path: str, maximum_bytes: int) -> AsyncIterator[bytes]: ...

    async def write(self, path: str, data: bytes) -> None: ...

    async def listdir(self, path: str, *, recursive: bool = False) -> Sequence[WorkspaceEntry]: ...

    async def provenance(self, path: str) -> WorkspaceProvenance: ...


class WorkspaceFactory(Protocol):
    def for_run(self, tenant_id: str, run_id: object, lease_epoch: int = 0) -> WorkspaceHandle: ...
