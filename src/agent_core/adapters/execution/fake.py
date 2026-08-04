"""Deterministic execution adapter; it records commands and executes no code."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import PurePosixPath
from uuid import UUID

from agent_core.adapters.execution.local_workspace import validated_workspace_components
from agent_core.domain.errors import (
    ExecutionRejected,
    WorkspaceReadLimitExceededError,
)
from agent_core.domain.execution import (
    BridgeEndpoint,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    KillReason,
    WorkspaceEntry,
    WorkspaceProvenance,
)
from agent_core.ports.determinism import Clock, IdFactory


class _FakeWorkspaceHandle:
    def __init__(self) -> None:
        self._files: dict[PurePosixPath, bytes] = {}
        self._provenance: dict[PurePosixPath, WorkspaceProvenance] = {}

    @property
    def root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath:
        return self.root.joinpath(*validated_workspace_components(path))

    def _relative(self, path: str | PurePosixPath) -> PurePosixPath:
        return self.resolve(path).relative_to(self.root)

    async def read(self, path: str) -> bytes:
        try:
            return self._files[self._relative(path)]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def read_bounded(self, path: str, maximum_bytes: int) -> bytes:
        if maximum_bytes < 0:
            raise ValueError("maximum_bytes must not be negative")
        data = await self.read(path)
        if len(data) > maximum_bytes:
            raise WorkspaceReadLimitExceededError("workspace file exceeds read limit")
        return data

    async def stream(self, path: str, maximum_bytes: int) -> AsyncIterator[bytes]:
        data = await self.read_bounded(path, maximum_bytes)
        for offset in range(0, len(data), 64 * 1024):
            yield data[offset : offset + 64 * 1024]

    async def write(self, path: str, data: bytes) -> None:
        relative = self._relative(path)
        self._files[relative] = data
        self._provenance[relative] = WorkspaceProvenance.TOOL_WRITTEN

    async def listdir(self, path: str, *, recursive: bool = False) -> tuple[WorkspaceEntry, ...]:
        base = self._relative(path)
        if base in self._files or any(parent in self._files for parent in base.parents):
            raise NotADirectoryError(path)
        if base.parts and not any(file_path.is_relative_to(base) for file_path in self._files):
            raise FileNotFoundError(path)
        entries: dict[PurePosixPath, WorkspaceEntry] = {}
        for file_path, data in self._files.items():
            try:
                relative = file_path.relative_to(base)
            except ValueError:
                continue
            if not recursive and len(relative.parts) > 1:
                relative = PurePosixPath(relative.parts[0])
            result_path = base / relative
            is_directory = result_path != file_path
            entries[result_path] = WorkspaceEntry(
                path=result_path,
                kind="directory" if is_directory else "file",
                size_bytes=0 if is_directory else len(data),
            )
        return tuple(entries[path] for path in sorted(entries, key=str))

    async def provenance(self, path: str) -> WorkspaceProvenance:
        return self._provenance.get(self._relative(path), WorkspaceProvenance.UNKNOWN)


class FakeExecutionEnvironment:
    """Scriptable adapter used by the port contract and deterministic harness."""

    def __init__(self, clock: Clock, ids: IdFactory) -> None:
        self._clock = clock
        self._ids = ids
        self.commands: list[tuple[EnvironmentHandle, ExecutionCommand]] = []
        self.specifications: list[EnvironmentSpec] = []
        self._live: dict[str, EnvironmentSpec] = {}
        self._workspaces: dict[str, _FakeWorkspaceHandle] = {}
        self._script: deque[ExecutionResult] = deque()

    def queue_result(self, result: ExecutionResult) -> None:
        self._script.append(result)

    async def provision(self, specification: EnvironmentSpec) -> EnvironmentHandle:
        now = self._clock.now()
        environment_id = str(self._ids.new_id())
        handle = EnvironmentHandle(
            environment_id=environment_id,
            tenant_id=specification.tenant_id,
            run_id=specification.run_id,
            lease_epoch=specification.lease_epoch,
            created_at=now,
            expires_at=now + timedelta(seconds=specification.limits.wall_clock_seconds),
        )
        self.specifications.append(specification)
        self._live[environment_id] = specification
        self._workspaces[environment_id] = _FakeWorkspaceHandle()
        return handle

    def workspace(self, environment: EnvironmentHandle) -> _FakeWorkspaceHandle:
        self._assert_live(environment)
        return self._workspaces[environment.environment_id]

    async def execute(
        self, environment: EnvironmentHandle, command: ExecutionCommand
    ) -> ExecutionResult:
        self._assert_live(environment)
        self.commands.append((environment, command))
        if self._script:
            return self._script.popleft()
        stdout = command.stdin or b""
        truncated = len(stdout) > command.maximum_output_bytes
        if truncated:
            stdout = stdout[: command.maximum_output_bytes]
        return ExecutionResult(
            exit_code=None if truncated else 0,
            stdout=stdout,
            stderr=b"",
            stdout_truncated=truncated,
            stderr_truncated=False,
            timed_out=False,
            killed_by=KillReason.OUTPUT_LIMIT if truncated else None,
            files_changed=(),
            duration_ms=0,
        )

    async def execute_with_bridge(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
        endpoint: BridgeEndpoint,
        handler: object,
    ) -> ExecutionResult:
        del endpoint, handler
        return await self.execute(environment, command)

    async def destroy(self, environment: EnvironmentHandle) -> None:
        self._live.pop(environment.environment_id, None)
        self._workspaces.pop(environment.environment_id, None)

    async def reap(
        self,
        live_leases: frozenset[tuple[object, int]],
        is_live: Callable[[UUID, int], Awaitable[bool]] | None = None,
    ) -> int:
        stale: list[str] = []
        for environment_id, specification in tuple(self._live.items()):
            lease = (specification.run_id, specification.lease_epoch)
            if lease in live_leases:
                continue
            if is_live is not None and await is_live(*lease):
                continue
            stale.append(environment_id)
        for environment_id in stale:
            self._live.pop(environment_id, None)
            self._workspaces.pop(environment_id, None)
        return len(stale)

    def live_environment_ids(self) -> frozenset[str]:
        return frozenset(self._live)

    def _assert_live(self, environment: EnvironmentHandle) -> None:
        specification = self._live.get(environment.environment_id)
        if specification is None:
            raise ExecutionRejected("execution environment is gone")
        if (
            specification.tenant_id != environment.tenant_id
            or specification.run_id != environment.run_id
            or specification.lease_epoch != environment.lease_epoch
        ):
            raise ExecutionRejected("execution handle does not match its environment")


def fake_image_digest() -> str:
    return "sha256:" + hashlib.sha256(b"agent-core-fake-sandbox-v1").hexdigest()
