"""Lease-scoped sandbox ownership and lazy workspace provisioning."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import PurePosixPath
from typing import Protocol, cast
from uuid import UUID

from agent_core.domain.execution import (
    BridgeEndpoint,
    EgressPolicy,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    ResourceLimits,
    WorkspaceEntry,
    WorkspaceProvenance,
)
from agent_core.execution.environment import build_sandbox_environment
from agent_core.ports.execution import ExecutionEnvironment, WorkspaceHandle


class _WorkspaceEnvironment(ExecutionEnvironment, Protocol):
    def workspace(self, environment: EnvironmentHandle) -> WorkspaceHandle: ...


class _BridgeHandler(Protocol):
    @property
    def token(self) -> str: ...

    async def handle(self, request: bytes) -> bytes: ...


class _BridgeExecutionEnvironment(_WorkspaceEnvironment, Protocol):
    async def execute_with_bridge(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
        endpoint: BridgeEndpoint,
        handler: _BridgeHandler,
    ) -> ExecutionResult: ...


class LeaseWorkspaceHandle:
    def __init__(
        self,
        manager: SandboxManager,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
    ) -> None:
        self._manager = manager
        self._tenant_id = tenant_id
        self._run_id = run_id
        self._lease_epoch = lease_epoch

    @property
    def root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath:
        # Containment validation is pure and does not need to provision a sandbox.
        from agent_core.adapters.execution.local_workspace import validated_workspace_components

        return self.root.joinpath(*validated_workspace_components(path))

    async def _delegate(self) -> WorkspaceHandle:
        return await self._manager.workspace_for(self._tenant_id, self._run_id, self._lease_epoch)

    async def read(self, path: str) -> bytes:
        return await (await self._delegate()).read(path)

    async def read_bounded(self, path: str, maximum_bytes: int) -> bytes:
        return await (await self._delegate()).read_bounded(path, maximum_bytes)

    async def stream(self, path: str, maximum_bytes: int) -> AsyncIterator[bytes]:
        delegate = await self._delegate()
        async for chunk in delegate.stream(path, maximum_bytes):
            yield chunk

    async def write(self, path: str, data: bytes) -> None:
        await (await self._delegate()).write(path, data)

    async def listdir(self, path: str, *, recursive: bool = False) -> tuple[WorkspaceEntry, ...]:
        return tuple(await (await self._delegate()).listdir(path, recursive=recursive))

    async def provenance(self, path: str) -> WorkspaceProvenance:
        return await (await self._delegate()).provenance(path)


class SandboxManager:
    """Own one environment for each run lease and erase it on release."""

    def __init__(
        self,
        environment: _WorkspaceEnvironment,
        *,
        image_digest: str | None = None,
        resolve_image_digest: Callable[[], Awaitable[str]] | None = None,
        limits: ResourceLimits,
        egress: EgressPolicy | None = None,
        parent_environment: Mapping[str, str] | None = None,
        passthrough_names: tuple[str, ...] = (),
    ) -> None:
        self._environment = environment
        self._image_digest = image_digest
        self._resolve_image_digest = resolve_image_digest
        self._limits = limits
        self._egress = egress or EgressPolicy()
        self._parent_environment = dict(parent_environment or {})
        self._passthrough_names = passthrough_names
        self._handles: dict[tuple[str, UUID, int], EnvironmentHandle] = {}
        self._locks: dict[tuple[str, UUID, int], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def for_run(self, tenant_id: str, run_id: object, lease_epoch: int = 0) -> LeaseWorkspaceHandle:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        return LeaseWorkspaceHandle(self, tenant_id, run_id, lease_epoch)

    async def _handle_for(
        self, tenant_id: str, run_id: UUID, lease_epoch: int
    ) -> EnvironmentHandle:
        key = (tenant_id, run_id, lease_epoch)
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = self._handles.get(key)
            if current is not None:
                return current
            if self._image_digest is None:
                if self._resolve_image_digest is None:
                    raise RuntimeError("sandbox image digest has no resolver")
                self._image_digest = await self._resolve_image_digest()
            spec = EnvironmentSpec(
                tenant_id=tenant_id,
                run_id=run_id,
                lease_epoch=lease_epoch,
                image_digest=self._image_digest,
                limits=self._limits,
                egress=self._egress,
                environment=build_sandbox_environment(
                    self._parent_environment, self._passthrough_names
                ),
            )
            handle = await self._environment.provision(spec)
            self._handles[key] = handle
            return handle

    async def workspace_for(
        self, tenant_id: str, run_id: UUID, lease_epoch: int
    ) -> WorkspaceHandle:
        handle = await self._handle_for(tenant_id, run_id, lease_epoch)
        return self._environment.workspace(handle)

    async def execute_for(
        self,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        command: ExecutionCommand,
        bridge: _BridgeHandler | None = None,
    ) -> ExecutionResult:
        handle = await self._handle_for(tenant_id, run_id, lease_epoch)
        if bridge is not None:
            bridge_environment = cast(_BridgeExecutionEnvironment, self._environment)
            endpoint = BridgeEndpoint(
                socket_path=PurePosixPath(f"/workspace/.agent/bridge-{secrets.token_hex(8)}.sock"),
                token=bridge.token,
            )
            return await bridge_environment.execute_with_bridge(handle, command, endpoint, bridge)
        return await self._environment.execute(handle, command)

    async def release_run(self, run_id: UUID) -> None:
        matches = [(key, handle) for key, handle in self._handles.items() if key[1] == run_id]
        errors: list[Exception] = []
        for key, handle in matches:
            try:
                await self._environment.destroy(handle)
            except Exception as exc:
                errors.append(exc)
            else:
                self._handles.pop(key, None)
                self._locks.pop(key, None)
        if errors:
            raise ExceptionGroup("one or more sandbox releases failed", errors)

    async def reap(self, live_leases: frozenset[tuple[UUID, int]]) -> int:
        reaper = getattr(self._environment, "reap", None)
        if reaper is None:
            return 0
        return int(await reaper(frozenset(live_leases)))

    async def close(self) -> None:
        errors: list[Exception] = []
        for key, handle in tuple(self._handles.items()):
            try:
                await self._environment.destroy(handle)
            except Exception as exc:
                errors.append(exc)
            else:
                self._handles.pop(key, None)
                self._locks.pop(key, None)
        if errors:
            raise ExceptionGroup("one or more sandbox closes failed", errors)

    @property
    def adapter(self) -> ExecutionEnvironment:
        return cast(ExecutionEnvironment, self._environment)
