"""Lease-scoped sandbox ownership and lazy workspace provisioning."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import PurePosixPath
from typing import Protocol, cast
from uuid import UUID

from agent_core.domain.errors import ExecutionRejected
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

    async def read(self, path: str) -> bytes:
        async with self._manager.workspace_operation(
            self._tenant_id, self._run_id, self._lease_epoch
        ) as workspace:
            return await workspace.read(path)

    async def read_bounded(self, path: str, maximum_bytes: int) -> bytes:
        async with self._manager.workspace_operation(
            self._tenant_id, self._run_id, self._lease_epoch
        ) as workspace:
            return await workspace.read_bounded(path, maximum_bytes)

    async def stream(self, path: str, maximum_bytes: int) -> AsyncIterator[bytes]:
        async with self._manager.workspace_operation(
            self._tenant_id, self._run_id, self._lease_epoch
        ) as workspace:
            async for chunk in workspace.stream(path, maximum_bytes):
                yield chunk

    async def write(self, path: str, data: bytes) -> None:
        async with self._manager.workspace_operation(
            self._tenant_id, self._run_id, self._lease_epoch
        ) as workspace:
            await workspace.write(path, data)

    async def listdir(self, path: str, *, recursive: bool = False) -> tuple[WorkspaceEntry, ...]:
        async with self._manager.workspace_operation(
            self._tenant_id, self._run_id, self._lease_epoch
        ) as workspace:
            return tuple(await workspace.listdir(path, recursive=recursive))

    async def provenance(self, path: str) -> WorkspaceProvenance:
        async with self._manager.workspace_operation(
            self._tenant_id, self._run_id, self._lease_epoch
        ) as workspace:
            return await workspace.provenance(path)


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
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        self._environment = environment
        self._image_digest = image_digest
        self._resolve_image_digest = resolve_image_digest
        self._limits = limits
        self._egress = egress or EgressPolicy()
        self._parent_environment = dict(parent_environment or {})
        self._passthrough_names = passthrough_names
        if drain_timeout_seconds <= 0:
            raise ValueError("sandbox drain timeout must be positive")
        self._drain_timeout_seconds = drain_timeout_seconds
        self._handles: dict[tuple[str, UUID, int], EnvironmentHandle] = {}
        self._locks: dict[tuple[str, UUID, int], asyncio.Lock] = {}
        self._lock_users: dict[tuple[str, UUID, int], int] = {}
        self._guard = asyncio.Lock()
        self._condition = asyncio.Condition(self._guard)
        self._active: dict[tuple[str, UUID, int], int] = {}
        self._released_runs: set[UUID] = set()
        self._closing = False
        self._teardown_lock = asyncio.Lock()

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
            self._lock_users[key] = self._lock_users.get(key, 0) + 1
        try:
            async with lock:
                if self._closing:
                    raise ExecutionRejected("sandbox manager is closing")
                if run_id in self._released_runs:
                    raise ExecutionRejected("sandbox run has been released")
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
                # No await may separate a live provisioned handle from manager
                # ownership; release and close rely on this handoff being atomic
                # with respect to asyncio task cancellation.
                self._handles[key] = handle
                if self._closing or run_id in self._released_runs:
                    reason = "closing" if self._closing else "released"
                    # The teardown flow snapshots and retries retained handles.
                    with suppress(Exception):
                        await self._destroy_matches(((key, handle),))
                    raise ExecutionRejected(f"sandbox run is {reason}")
                return handle
        finally:
            async with self._condition:
                remaining = self._lock_users.get(key, 1) - 1
                if remaining:
                    self._lock_users[key] = remaining
                else:
                    self._lock_users.pop(key, None)
                    if key not in self._handles:
                        self._locks.pop(key, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def _operation(
        self, tenant_id: str, run_id: UUID, lease_epoch: int
    ) -> AsyncIterator[EnvironmentHandle]:
        key = (tenant_id, run_id, lease_epoch)
        async with self._condition:
            if self._closing:
                raise ExecutionRejected("sandbox manager is closing")
            if run_id in self._released_runs:
                raise ExecutionRejected("sandbox run has been released")
            self._active[key] = self._active.get(key, 0) + 1
        try:
            yield await self._handle_for(tenant_id, run_id, lease_epoch)
        finally:
            async with self._condition:
                remaining = self._active[key] - 1
                if remaining:
                    self._active[key] = remaining
                else:
                    self._active.pop(key, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def workspace_operation(
        self, tenant_id: str, run_id: UUID, lease_epoch: int
    ) -> AsyncIterator[WorkspaceHandle]:
        async with self._operation(tenant_id, run_id, lease_epoch) as handle:
            yield self._environment.workspace(handle)

    async def execute_for(
        self,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        command: ExecutionCommand,
        bridge: _BridgeHandler | None = None,
    ) -> ExecutionResult:
        async with self._operation(tenant_id, run_id, lease_epoch) as handle:
            if bridge is not None:
                bridge_environment = cast(_BridgeExecutionEnvironment, self._environment)
                endpoint = BridgeEndpoint(
                    socket_path=PurePosixPath(
                        f"/workspace/.agent/bridge-{secrets.token_hex(8)}.sock"
                    ),
                    token=bridge.token,
                )
                return await bridge_environment.execute_with_bridge(
                    handle, command, endpoint, bridge
                )
            return await self._environment.execute(handle, command)

    async def _destroy_matches(
        self, matches: tuple[tuple[tuple[str, UUID, int], EnvironmentHandle], ...]
    ) -> None:
        errors: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        for key, handle in matches:
            try:
                await self._environment.destroy(handle)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception as exc:
                errors.append(exc)
            else:
                async with self._guard:
                    if self._handles.get(key) == handle:
                        self._handles.pop(key, None)
                        if self._lock_users.get(key, 0) == 0:
                            self._locks.pop(key, None)
        if cancelled is not None:
            raise cancelled
        if errors:
            raise ExceptionGroup("one or more sandbox teardowns failed", errors)

    async def _discard_unused_locks(self, run_id: UUID | None = None) -> None:
        async with self._guard:
            for key in tuple(self._locks):
                if (
                    key not in self._handles
                    and self._lock_users.get(key, 0) == 0
                    and (run_id is None or key[1] == run_id)
                ):
                    self._locks.pop(key, None)
                    self._lock_users.pop(key, None)

    @staticmethod
    async def _await_teardown(task: asyncio.Task[None]) -> None:
        caller_cancellation: asyncio.CancelledError | None = None
        teardown_failure: BaseException | None = None
        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.done():
                    teardown_failure = exc
                    break
                caller_cancellation = caller_cancellation or exc
            except BaseException as exc:
                teardown_failure = exc
                break
            else:
                break
        if caller_cancellation is not None:
            raise caller_cancellation
        if teardown_failure is not None:
            raise teardown_failure

    async def _release_run(self, run_id: UUID) -> None:
        async with self._condition:
            self._released_runs.add(run_id)
        async with self._teardown_lock:
            async with self._condition:
                with suppress(TimeoutError):
                    async with asyncio.timeout(self._drain_timeout_seconds):
                        await self._condition.wait_for(
                            lambda: not any(key[1] == run_id for key in self._active)
                        )
                await self._condition.wait_for(
                    lambda: not any(key[1] == run_id for key in self._lock_users)
                )
                matches = tuple(
                    (key, handle) for key, handle in self._handles.items() if key[1] == run_id
                )
            try:
                await self._destroy_matches(matches)
            finally:
                await self._discard_unused_locks(run_id)

    async def release_run(self, run_id: UUID) -> None:
        await self._await_teardown(asyncio.create_task(self._release_run(run_id)))

    async def reap(self, live_leases: frozenset[tuple[UUID, int]]) -> int:
        reaper = getattr(self._environment, "reap", None)
        if reaper is None:
            return 0
        return int(await reaper(frozenset(live_leases)))

    async def _close(self) -> None:
        async with self._condition:
            self._closing = True
            with suppress(TimeoutError):
                async with asyncio.timeout(self._drain_timeout_seconds):
                    await self._condition.wait_for(lambda: not self._active)
            await self._condition.wait_for(lambda: not self._lock_users)
        async with self._teardown_lock:
            try:
                await self._destroy_matches(tuple(self._handles.items()))
            finally:
                await self._discard_unused_locks()

    async def close(self) -> None:
        await self._await_teardown(asyncio.create_task(self._close()))

    @property
    def adapter(self) -> ExecutionEnvironment:
        return cast(ExecutionEnvironment, self._environment)
