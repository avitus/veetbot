from __future__ import annotations

import asyncio
import logging
import signal
import stat
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast
from uuid import UUID

import pytest

from agent_core import bootstrap
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution import docker as docker_adapter
from agent_core.adapters.execution import service as service_adapter
from agent_core.adapters.execution.docker import DockerExecutionEnvironment
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.adapters.execution.service import (
    ExecutionServiceClient,
    ExecutionServiceServer,
    ExecutionServiceWorkspace,
    _decode,
)
from agent_core.domain.errors import (
    ExecutionRejected,
    ExecutionUnavailable,
    WorkspaceEscape,
)
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


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="veetbot-exec-") as directory:
        yield Path(directory)


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
async def execution_service(
    socket_dir: Path,
) -> AsyncIterator[tuple[ExecutionServiceClient, FakeExecutionEnvironment]]:
    socket_path = socket_dir / "execution.sock"
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
async def test_execution_service_socket_is_readable_and_writable_only_by_its_group(
    socket_dir: Path,
) -> None:
    """ADR-0067 decision 3: the socket is group-readable and writable only by the
    application group, so the mode after start() must be exactly 0660."""
    socket_path = socket_dir / "execution.sock"
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())

    async def resolve(reference: str) -> str:
        return fake_image_digest()

    server = ExecutionServiceServer(environment, socket_path, resolve_image_digest=resolve)
    await server.start()
    try:
        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
        assert stat.S_IMODE(socket_path.lstat().st_mode) == 0o660
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
async def test_execution_service_relays_bridge_callbacks_without_runtime_access(
    socket_dir: Path,
) -> None:
    socket_path = socket_dir / "execution.sock"
    environment = _BridgeEnvironment(FixedClock(NOW), SequenceIdFactory())

    async def resolve(_reference: str) -> str:
        return fake_image_digest()

    server = ExecutionServiceServer(
        environment,
        socket_path,
        resolve_image_digest=resolve,
    )
    await server.start()
    try:
        client = ExecutionServiceClient(socket_path)
        handle = await client.provision(_spec(UUID(int=102)))

        class Handler:
            async def handle(self, request: bytes) -> bytes:
                assert request == b"bridge request"
                return b"bridge response"

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


def test_execution_service_rejects_relative_socket_paths() -> None:
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())

    async def resolve(_reference: str) -> str:
        return fake_image_digest()

    with pytest.raises(ValueError, match="absolute"):
        ExecutionServiceClient(Path("relative.sock"))
    with pytest.raises(ValueError, match="absolute"):
        ExecutionServiceServer(
            environment,
            Path("relative.sock"),
            resolve_image_digest=resolve,
        )


@pytest.mark.asyncio
async def test_execution_service_refuses_to_replace_unsafe_socket_path(tmp_path: Path) -> None:
    socket_path = tmp_path / "execution.sock"
    socket_path.write_text("occupied", encoding="utf-8")
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())

    async def resolve(_reference: str) -> str:
        return fake_image_digest()

    server = ExecutionServiceServer(
        environment,
        socket_path,
        resolve_image_digest=resolve,
    )
    with pytest.raises(ExecutionUnavailable, match="unsafe"):
        await server.start()
    assert socket_path.read_text(encoding="utf-8") == "occupied"


@pytest.mark.asyncio
async def test_execution_service_preserves_remote_boundary_errors(
    execution_service: tuple[ExecutionServiceClient, FakeExecutionEnvironment],
) -> None:
    client, _environment = execution_service
    handle = await client.provision(_spec(UUID(int=105)))
    workspace = client.workspace(handle)

    with pytest.raises(FileNotFoundError):
        await workspace.read("missing.txt")
    with pytest.raises(WorkspaceEscape):
        await workspace.read("../outside.txt")
    with pytest.raises(ExecutionRejected, match="unknown execution service operation"):
        await client._call("unknown", {})


@pytest.mark.asyncio
async def test_execution_service_closed_socket_is_unavailable(
    socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = socket_dir / "execution.sock"
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())

    async def resolve(_reference: str) -> str:
        return fake_image_digest()

    server = ExecutionServiceServer(
        environment,
        socket_path,
        resolve_image_digest=resolve,
    )
    await server.start()
    await server.close()
    monkeypatch.setattr(service_adapter, "_CONNECT_ATTEMPTS", 1)

    with pytest.raises(ExecutionUnavailable, match="socket is unavailable"):
        await ExecutionServiceClient(socket_path).resolve_image_digest("image:tag")


@pytest.mark.asyncio
async def test_execution_service_connect_retries_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def unavailable(_path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        nonlocal attempts
        attempts += 1
        raise OSError("unavailable")

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(service_adapter, "_CONNECT_ATTEMPTS", 3)
    monkeypatch.setattr(service_adapter, "_CONNECT_RETRY_SECONDS", 0)
    monkeypatch.setattr(asyncio, "open_unix_connection", unavailable)
    monkeypatch.setattr(asyncio, "sleep", no_wait)

    with pytest.raises(ExecutionUnavailable, match="socket is unavailable"):
        await ExecutionServiceClient(tmp_path / "missing.sock")._connect()
    assert attempts == 3


def test_execution_service_decode_rejects_short_mapping_pairs() -> None:
    with pytest.raises(ValueError, match="invalid mapping"):
        _decode({"__mapping__": [["missing-value"]]})


@pytest.mark.asyncio
async def test_execution_service_socket_exchange_has_a_deadline(
    socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = socket_dir / "hung.sock"
    release = asyncio.Event()

    async def hang(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(hang, path=str(socket_path))
    monkeypatch.setattr(service_adapter, "_REQUEST_TIMEOUT_SECONDS", 0.01)
    try:
        with pytest.raises(ExecutionUnavailable, match="timed out"):
            await asyncio.wait_for(
                ExecutionServiceClient(socket_path).resolve_image_digest("image:tag"),
                timeout=0.2,
            )
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unbounded_workspace_read_uses_the_bounded_stream() -> None:
    class StreamingOwner:
        operation: str | None = None
        payload: Mapping[str, object] = {}

        async def _call(self, _operation: str, _payload: Mapping[str, object]) -> object:
            raise AssertionError("workspace.read must not use one unbounded response frame")

        async def _stream(
            self,
            operation: str,
            payload: Mapping[str, object],
        ) -> AsyncIterator[bytes]:
            self.operation = operation
            self.payload = payload
            yield b"bounded "
            yield b"read"

    owner = StreamingOwner()
    handle = EnvironmentHandle(
        "environment",
        "tenant-a",
        UUID(int=106),
        1,
        NOW,
        NOW,
    )
    workspace = ExecutionServiceWorkspace(cast(ExecutionServiceClient, owner), handle)

    assert await workspace.read("result.bin") == b"bounded read"
    assert owner.operation == "workspace_stream"
    assert owner.payload["maximum_bytes"] == 512 * 1024 * 1024


@pytest.mark.asyncio
async def test_execution_service_logs_failure_when_error_response_disconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())

    async def resolve(_reference: str) -> str:
        return fake_image_digest()

    server = ExecutionServiceServer(
        environment,
        tmp_path / "execution.sock",
        resolve_image_digest=resolve,
    )

    async def request(_reader: asyncio.StreamReader) -> dict[str, object]:
        return {
            "kind": "request",
            "operation": "explode",
            "payload": {"__mapping__": []},
        }

    async def explode(
        _operation: str,
        _payload: dict[str, object],
        _callback: object,
    ) -> object:
        raise RuntimeError("programming error")

    class BrokenWriter:
        def write(self, _data: bytes) -> None:
            raise BrokenPipeError("client disconnected")

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    monkeypatch.setattr(service_adapter, "_read_frame", request)
    monkeypatch.setattr(server, "_dispatch", explode)
    with caplog.at_level(logging.ERROR, logger=service_adapter.__name__):
        await server._handle_client(
            cast(asyncio.StreamReader, object()),
            cast(asyncio.StreamWriter, BrokenWriter()),
        )
    assert "execution_service_request_failed" in caplog.text


@pytest.mark.asyncio
async def test_docker_execution_environment_close_discards_every_tracked_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = DockerExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    first = _spec(UUID(int=107))
    second = _spec(UUID(int=108))
    environment._states = {
        "first": docker_adapter._DockerState(first, "container-1", "volume-1"),
        "second": docker_adapter._DockerState(second, "container-2", "volume-2"),
    }
    discarded: list[tuple[str, str]] = []

    async def discard(
        container: str,
        volume: str,
        _proxy: str | None,
        _network: str | None,
    ) -> None:
        discarded.append((container, volume))

    monkeypatch.setattr(environment, "_discard", discard)

    await environment.close()

    assert discarded == [("container-1", "volume-1"), ("container-2", "volume-2")]
    assert environment.live_environment_ids() == frozenset()


@pytest.mark.asyncio
async def test_execution_service_composition_closes_server_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Runtime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("runtime-created")

        async def close(self) -> None:
            events.append("runtime-closed")

    class Server:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("server-created")

        async def serve_forever(self) -> None:
            events.append("served")

        async def close(self) -> None:
            events.append("server-closed")

    monkeypatch.setattr(bootstrap, "DockerExecutionEnvironment", Runtime)
    monkeypatch.setattr(bootstrap, "ExecutionServiceServer", Server)

    await bootstrap.serve_execution_service(tmp_path / "execution.sock")

    assert events == [
        "runtime-created",
        "server-created",
        "served",
        "server-closed",
        "runtime-closed",
    ]


@pytest.mark.asyncio
async def test_execution_service_signal_runs_graceful_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    blocked = asyncio.Event()
    handlers: dict[signal.Signals, object] = {}
    removed: list[signal.Signals] = []
    events: list[str] = []

    class Runtime:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def close(self) -> None:
            events.append("runtime-closed")

    class Server:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def serve_forever(self) -> None:
            started.set()
            await blocked.wait()

        async def close(self) -> None:
            events.append("server-closed")

    loop = asyncio.get_running_loop()

    def add_signal_handler(
        signal_number: signal.Signals,
        callback: object,
        *_args: object,
    ) -> None:
        handlers[signal_number] = callback

    def remove_signal_handler(signal_number: signal.Signals) -> bool:
        removed.append(signal_number)
        return True

    monkeypatch.setattr(bootstrap, "DockerExecutionEnvironment", Runtime)
    monkeypatch.setattr(bootstrap, "ExecutionServiceServer", Server)
    monkeypatch.setattr(loop, "add_signal_handler", add_signal_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", remove_signal_handler)

    serving = asyncio.create_task(bootstrap.serve_execution_service(tmp_path / "execution.sock"))
    await started.wait()
    try:
        assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
        callback = cast(Callable[[], None], handlers[signal.SIGTERM])
        callback()
        await asyncio.wait_for(serving, timeout=0.2)
    finally:
        if not serving.done():
            serving.cancel()
            with suppress(asyncio.CancelledError):
                await serving

    assert events == ["server-closed", "runtime-closed"]
    assert set(removed) == {signal.SIGINT, signal.SIGTERM}


@pytest.mark.parametrize(
    ("operation", "payload", "field"),
    [
        ("resolve_image_digest", {"reference": 7}, "reference"),
        ("provision", {"specification": "invalid"}, "specification"),
        (
            "execute",
            {"environment": "invalid", "command": _command()},
            "environment",
        ),
        (
            "execute",
            {
                "environment": EnvironmentHandle(
                    "environment", "tenant-a", UUID(int=109), 1, NOW, NOW
                ),
                "command": "invalid",
            },
            "command",
        ),
        (
            "execute_with_bridge",
            {
                "environment": EnvironmentHandle(
                    "environment", "tenant-a", UUID(int=110), 1, NOW, NOW
                ),
                "command": _command(),
                "endpoint": "invalid",
            },
            "endpoint",
        ),
        (
            "workspace_read_bounded",
            {
                "environment": EnvironmentHandle(
                    "environment", "tenant-a", UUID(int=111), 1, NOW, NOW
                ),
                "path": 7,
                "maximum_bytes": 10,
            },
            "path",
        ),
        (
            "workspace_write",
            {
                "environment": EnvironmentHandle(
                    "environment", "tenant-a", UUID(int=112), 1, NOW, NOW
                ),
                "path": "result.txt",
                "data": "invalid",
            },
            "data",
        ),
        (
            "workspace_stream",
            {
                "environment": EnvironmentHandle(
                    "environment", "tenant-a", UUID(int=113), 1, NOW, NOW
                ),
                "path": "result.txt",
                "maximum_bytes": "invalid",
            },
            "maximum_bytes",
        ),
        (
            "workspace_listdir",
            {
                "environment": EnvironmentHandle(
                    "environment", "tenant-a", UUID(int=114), 1, NOW, NOW
                ),
                "path": ".",
                "recursive": 1,
            },
            "recursive",
        ),
        (
            "workspace_provenance",
            {
                "environment": EnvironmentHandle(
                    "environment", "tenant-a", UUID(int=115), 1, NOW, NOW
                )
            },
            "path",
        ),
        ("reap", {"live_leases": []}, "live_leases"),
        ("reap", {"live_leases": frozenset({("invalid", 1)})}, "live_leases"),
    ],
)
@pytest.mark.asyncio
async def test_execution_service_rejects_malformed_operation_payloads(
    execution_service: tuple[ExecutionServiceClient, FakeExecutionEnvironment],
    operation: str,
    payload: dict[str, object],
    field: str,
) -> None:
    client, _environment = execution_service

    with pytest.raises(ExecutionRejected, match=rf"field {field} is invalid"):
        await client._call(operation, payload)


def test_execution_service_has_no_unused_peer_credential_api() -> None:
    assert not hasattr(service_adapter, "unix_peer_credentials")
