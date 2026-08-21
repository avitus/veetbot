from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.adapters.execution.service import (
    ExecutionServiceClient,
    ExecutionServiceServer,
)
from agent_core.domain.errors import ExecutionRejected
from agent_core.domain.execution import (
    BridgeEndpoint,
    EgressDestination,
    EgressMode,
    EgressPolicy,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    ResourceLimits,
    WorkspaceProvenance,
)

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _spec(run_id: UUID) -> EnvironmentSpec:
    return EnvironmentSpec(
        tenant_id="tenant-a",
        run_id=run_id,
        lease_epoch=3,
        image_digest=fake_image_digest(),
        limits=ResourceLimits(
            cpu_millicores=1000,
            memory_bytes=128 * 1024 * 1024,
            pids_max=64,
            workspace_bytes=1024 * 1024,
            inodes_max=100,
            wall_clock_seconds=60,
        ),
        egress=EgressPolicy(
            mode=EgressMode.ALLOWLIST,
            destinations=(EgressDestination("example.com", frozenset({443})),),
        ),
        environment={"SAFE_VALUE": "present"},
    )


def _command(stdin: bytes = b"hello") -> ExecutionCommand:
    return ExecutionCommand(
        argv=("python", "-V"),
        working_directory=PurePosixPath("/workspace"),
        timeout_seconds=30,
        stdin=stdin,
        maximum_output_bytes=1024,
    )


@pytest.fixture
async def execution_service() -> AsyncIterator[
    tuple[ExecutionServiceClient, FakeExecutionEnvironment]
]:
    socket_path = Path("/tmp") / f"veetbot-execution-{uuid4().hex}.sock"
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())

    async def resolve(reference: str) -> str:
        assert reference == "agent-core-sandbox:production"
        return fake_image_digest()

    server = ExecutionServiceServer(
        environment,
        socket_path,
        resolve_image_digest=resolve,
    )
    await server.start()
    try:
        yield ExecutionServiceClient(socket_path), environment
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_execution_service_round_trips_runtime_and_workspace_operations(
    execution_service: tuple[ExecutionServiceClient, FakeExecutionEnvironment],
) -> None:
    client, environment = execution_service
    run_id = UUID(int=101)
    specification = _spec(run_id)

    assert await client.resolve_image_digest("agent-core-sandbox:production") == fake_image_digest()
    handle = await client.provision(specification)
    assert environment.specifications == [specification]

    workspace = client.workspace(handle)
    await workspace.write("notes/result.txt", b"stored")
    assert await workspace.read("notes/result.txt") == b"stored"
    assert await workspace.read_bounded("notes/result.txt", 6) == b"stored"
    assert await workspace.provenance("notes/result.txt") is WorkspaceProvenance.TOOL_WRITTEN
    assert [entry.path for entry in await workspace.listdir("notes")] == [
        PurePosixPath("notes/result.txt")
    ]
    assert b"".join([chunk async for chunk in workspace.stream("notes/result.txt", 6)]) == b"stored"

    result = await client.execute(handle, _command())
    assert result.stdout == b"hello"
    assert result.exit_code == 0

    await client.destroy(handle)
    with pytest.raises(ExecutionRejected, match="gone"):
        await client.execute(handle, _command())


class _BridgeEnvironment(FakeExecutionEnvironment):
    async def execute_with_bridge(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
        endpoint: BridgeEndpoint,
        handler: object,
    ) -> ExecutionResult:
        del endpoint
        response = await handler.handle(b"bridge request")  # type: ignore[attr-defined]
        return await self.execute(environment, _command(response))


@pytest.mark.asyncio
async def test_execution_service_relays_bridge_callbacks_without_runtime_access() -> None:
    socket_path = Path("/tmp") / f"veetbot-execution-{uuid4().hex}.sock"
    environment = _BridgeEnvironment(FixedClock(NOW), SequenceIdFactory())

    async def resolve(_reference: str) -> str:
        return fake_image_digest()

    server = ExecutionServiceServer(
        environment,
        socket_path,
        resolve_image_digest=resolve,
    )
    await server.start()
    client = ExecutionServiceClient(socket_path)
    handle = await client.provision(_spec(UUID(int=102)))

    class Handler:
        async def handle(self, request: bytes) -> bytes:
            assert request == b"bridge request"
            return b"bridge response"

    try:
        result = await client.execute_with_bridge(
            handle,
            _command(),
            BridgeEndpoint(PurePosixPath("/workspace/.agent/bridge.sock"), "secret"),
            Handler(),
        )
        assert result.stdout == b"bridge response"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_execution_service_reap_uses_worker_lease_callback(
    execution_service: tuple[ExecutionServiceClient, FakeExecutionEnvironment],
) -> None:
    client, _environment = execution_service
    retained = await client.provision(_spec(UUID(int=103)))
    removed = await client.provision(_spec(UUID(int=104)))

    async def is_live(run_id: UUID, lease_epoch: int) -> bool:
        assert lease_epoch == 3
        return run_id == retained.run_id

    assert await client.reap(frozenset(), is_live) == 1
    assert (await client.execute(retained, _command())).exit_code == 0
    with pytest.raises(ExecutionRejected, match="gone"):
        await client.execute(removed, _command())
