"""Shared execution-environment semantics exercised against the deterministic adapter."""

import asyncio
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.domain.errors import ExecutionRejected
from agent_core.domain.execution import (
    EgressPolicy,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    ResourceLimits,
)
from agent_core.execution.manager import SandboxManager


def _limits() -> ResourceLimits:
    return ResourceLimits(1000, 64 * 1024 * 1024, 32, 1024 * 1024, 100, 30)


class _FailingDestroyEnvironment(FakeExecutionEnvironment):
    def __init__(self, clock: FixedClock, ids: SequenceIdFactory) -> None:
        super().__init__(clock, ids)
        self.fail_next_destroy = True
        self.destroy_attempts: list[str] = []

    async def destroy(self, environment: EnvironmentHandle) -> None:
        self.destroy_attempts.append(environment.environment_id)
        if self.fail_next_destroy:
            self.fail_next_destroy = False
            raise RuntimeError("synthetic destroy failure")
        await super().destroy(environment)


class _CancellingDestroyEnvironment(_FailingDestroyEnvironment):
    async def destroy(self, environment: EnvironmentHandle) -> None:
        self.destroy_attempts.append(environment.environment_id)
        if self.fail_next_destroy:
            self.fail_next_destroy = False
            raise asyncio.CancelledError
        await FakeExecutionEnvironment.destroy(self, environment)


class _BlockingExecutionEnvironment(FakeExecutionEnvironment):
    def __init__(self, clock: FixedClock, ids: SequenceIdFactory) -> None:
        super().__init__(clock, ids)
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def execute(
        self, environment: EnvironmentHandle, command: ExecutionCommand
    ) -> ExecutionResult:
        self.started.set()
        await self.proceed.wait()
        return await super().execute(environment, command)


class _BlockingDestroyEnvironment(FakeExecutionEnvironment):
    def __init__(self, clock: FixedClock, ids: SequenceIdFactory) -> None:
        super().__init__(clock, ids)
        self.destroy_started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def destroy(self, environment: EnvironmentHandle) -> None:
        self.destroy_started.set()
        await self.proceed.wait()
        await super().destroy(environment)


class _FailingProvisionEnvironment(FakeExecutionEnvironment):
    async def provision(self, specification: EnvironmentSpec) -> EnvironmentHandle:
        del specification
        raise RuntimeError("synthetic provision failure")


class _BlockingProvisionEnvironment(FakeExecutionEnvironment):
    def __init__(self, clock: FixedClock, ids: SequenceIdFactory) -> None:
        super().__init__(clock, ids)
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def provision(self, specification: EnvironmentSpec) -> EnvironmentHandle:
        self.started.set()
        await self.proceed.wait()
        return await super().provision(specification)


async def test_execution_environment_lifecycle_stdin_and_output_limit() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = FakeExecutionEnvironment(clock, SequenceIdFactory())
    specification = EnvironmentSpec(
        tenant_id="tenant-a",
        run_id=UUID(int=10),
        lease_epoch=2,
        image_digest=fake_image_digest(),
        limits=_limits(),
        egress=EgressPolicy(),
        environment={"LANG": "C.UTF-8"},
    )
    handle = await adapter.provision(specification)
    result = await adapter.execute(
        handle,
        ExecutionCommand(("python",), PurePosixPath(""), 5, b"abcdef", 4),
    )
    assert result.stdout == b"abcd"
    assert result.stdout_truncated is True
    assert result.killed_by is not None
    await adapter.destroy(handle)
    await adapter.destroy(handle)
    with pytest.raises(ExecutionRejected):
        await adapter.execute(
            handle,
            ExecutionCommand(("true",), PurePosixPath(""), 1, None, 10),
        )


async def test_execution_environment_rejects_mismatched_handle() -> None:
    adapter = FakeExecutionEnvironment(
        FixedClock(datetime(2026, 1, 1, tzinfo=UTC)), SequenceIdFactory()
    )
    handle = await adapter.provision(
        EnvironmentSpec(
            "tenant-a", UUID(int=11), 1, fake_image_digest(), _limits(), EgressPolicy(), {}
        )
    )
    mismatched = EnvironmentHandle(
        handle.environment_id,
        "tenant-b",
        handle.run_id,
        handle.lease_epoch,
        handle.created_at,
        handle.expires_at,
    )
    with pytest.raises(ExecutionRejected):
        await adapter.execute(
            mismatched,
            ExecutionCommand(("true",), PurePosixPath(""), 1, None, 10),
        )
    await adapter.destroy(handle)


async def test_fake_workspace_listdir_matches_filesystem_error_semantics() -> None:
    adapter = FakeExecutionEnvironment(
        FixedClock(datetime(2026, 1, 1, tzinfo=UTC)), SequenceIdFactory()
    )
    handle = await adapter.provision(
        EnvironmentSpec(
            "tenant-a", UUID(int=12), 1, fake_image_digest(), _limits(), EgressPolicy(), {}
        )
    )
    workspace = adapter.workspace(handle)
    await workspace.write("notes/a.txt", b"a")
    with pytest.raises(NotADirectoryError):
        await workspace.listdir("notes/a.txt")
    with pytest.raises(NotADirectoryError):
        await workspace.listdir("notes/a.txt/child")
    with pytest.raises(FileNotFoundError):
        await workspace.listdir("missing")


async def test_sandbox_release_attempts_all_handles_and_retains_failures() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _FailingDestroyEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(
        adapter,
        image_digest=fake_image_digest(),
        limits=_limits(),
    )
    run_id = UUID(int=13)
    await manager.for_run("tenant-a", run_id, 1).write("one", b"1")
    await manager.for_run("tenant-a", run_id, 2).write("two", b"2")
    with pytest.raises(ExceptionGroup):
        await manager.release_run(run_id)
    assert len(adapter.destroy_attempts) == 2
    assert len(adapter.live_environment_ids()) == 1
    await manager.release_run(run_id)
    assert adapter.live_environment_ids() == frozenset()


async def test_sandbox_release_attempts_all_handles_before_propagating_cancellation() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _CancellingDestroyEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(adapter, image_digest=fake_image_digest(), limits=_limits())
    run_id = UUID(int=14)
    await manager.for_run("tenant-a", run_id, 1).write("one", b"1")
    await manager.for_run("tenant-a", run_id, 2).write("two", b"2")
    with pytest.raises(asyncio.CancelledError):
        await manager.release_run(run_id)
    assert len(adapter.destroy_attempts) == 2
    assert len(adapter.live_environment_ids()) == 1
    await manager.release_run(run_id)
    assert adapter.live_environment_ids() == frozenset()


async def test_sandbox_close_attempts_all_handles_before_propagating_cancellation() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _CancellingDestroyEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(adapter, image_digest=fake_image_digest(), limits=_limits())
    await manager.for_run("tenant-a", UUID(int=140), 1).write("one", b"1")
    await manager.for_run("tenant-a", UUID(int=141), 1).write("two", b"2")
    with pytest.raises(asyncio.CancelledError):
        await manager.close()
    assert len(adapter.destroy_attempts) == 2
    assert len(adapter.live_environment_ids()) == 1
    await manager.close()
    assert adapter.live_environment_ids() == frozenset()


async def test_sandbox_release_waits_for_active_operations_and_blocks_new_ones() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _BlockingExecutionEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(adapter, image_digest=fake_image_digest(), limits=_limits())
    run_id = UUID(int=15)
    execution = asyncio.create_task(
        manager.execute_for(
            "tenant-a",
            run_id,
            1,
            ExecutionCommand(("true",), PurePosixPath(""), 1, None, 10),
        )
    )
    await adapter.started.wait()
    release = asyncio.create_task(manager.release_run(run_id))
    while run_id not in manager._released_runs:
        await asyncio.sleep(0)
    assert release.done() is False
    with pytest.raises(ExecutionRejected, match="released"):
        await manager.for_run("tenant-a", run_id, 1).read("new")
    adapter.proceed.set()
    await execution
    await release
    assert adapter.live_environment_ids() == frozenset()


async def test_sandbox_close_waits_for_active_operations_and_blocks_provisioning() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _BlockingExecutionEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(adapter, image_digest=fake_image_digest(), limits=_limits())
    execution = asyncio.create_task(
        manager.execute_for(
            "tenant-a",
            UUID(int=16),
            1,
            ExecutionCommand(("true",), PurePosixPath(""), 1, None, 10),
        )
    )
    await adapter.started.wait()
    closing = asyncio.create_task(manager.close())
    while not manager._closing:
        await asyncio.sleep(0)
    with pytest.raises(ExecutionRejected, match="closing"):
        await manager.for_run("tenant-a", UUID(int=17), 1).read("new")
    adapter.proceed.set()
    await execution
    await closing
    assert adapter.live_environment_ids() == frozenset()


async def test_sandbox_release_defers_caller_cancellation_until_teardown() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _BlockingExecutionEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(adapter, image_digest=fake_image_digest(), limits=_limits())
    run_id = UUID(int=18)
    execution = asyncio.create_task(
        manager.execute_for(
            "tenant-a",
            run_id,
            1,
            ExecutionCommand(("true",), PurePosixPath(""), 1, None, 10),
        )
    )
    await adapter.started.wait()
    release = asyncio.create_task(manager.release_run(run_id))
    while run_id not in manager._released_runs:
        await asyncio.sleep(0)
    release.cancel()
    await asyncio.sleep(0)
    assert release.done() is False
    adapter.proceed.set()
    await execution
    with pytest.raises(asyncio.CancelledError):
        await release
    assert adapter.live_environment_ids() == frozenset()


async def test_sandbox_close_defers_caller_cancellation_until_teardown() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _BlockingExecutionEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(adapter, image_digest=fake_image_digest(), limits=_limits())
    execution = asyncio.create_task(
        manager.execute_for(
            "tenant-a",
            UUID(int=19),
            1,
            ExecutionCommand(("true",), PurePosixPath(""), 1, None, 10),
        )
    )
    await adapter.started.wait()
    closing = asyncio.create_task(manager.close())
    while not manager._closing:
        await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert closing.done() is False
    adapter.proceed.set()
    await execution
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert adapter.live_environment_ids() == frozenset()


async def test_sandbox_release_marks_a_run_before_unrelated_teardown() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _BlockingDestroyEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(adapter, image_digest=fake_image_digest(), limits=_limits())
    first_run = UUID(int=20)
    second_run = UUID(int=21)
    await manager.for_run("tenant-a", first_run, 1).write("first", b"1")
    await manager.for_run("tenant-a", second_run, 1).write("second", b"2")
    first_release = asyncio.create_task(manager.release_run(first_run))
    await adapter.destroy_started.wait()
    second_release = asyncio.create_task(manager.release_run(second_run))
    while second_run not in manager._released_runs:
        await asyncio.sleep(0)
    with pytest.raises(ExecutionRejected, match="released"):
        await manager.for_run("tenant-a", second_run, 1).read("second")
    adapter.proceed.set()
    await asyncio.gather(first_release, second_release)
    assert adapter.live_environment_ids() == frozenset()


async def test_sandbox_failed_provision_discards_its_uncached_lock() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _FailingProvisionEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(adapter, image_digest=fake_image_digest(), limits=_limits())
    run_id = UUID(int=22)
    with pytest.raises(RuntimeError, match="provision failure"):
        await manager.for_run("tenant-a", run_id, 1).write("never", b"data")
    assert manager._locks == {}
    await manager.release_run(run_id)
    assert manager._locks == {}


async def test_sandbox_release_bounds_an_abandoned_workspace_stream() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = FakeExecutionEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(
        adapter,
        image_digest=fake_image_digest(),
        limits=_limits(),
        drain_timeout_seconds=0.01,
    )
    run_id = UUID(int=23)
    workspace = manager.for_run("tenant-a", run_id, 1)
    await workspace.write("large", b"x" * (128 * 1024))
    stream = workspace.stream("large", 128 * 1024)
    assert len(await anext(stream)) == 64 * 1024
    await asyncio.wait_for(manager.release_run(run_id), timeout=0.2)
    assert adapter.live_environment_ids() == frozenset()
    await stream.aclose()  # type: ignore[attr-defined]


async def test_sandbox_release_reconciles_provisioning_after_drain_timeout() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _BlockingProvisionEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(
        adapter,
        image_digest=fake_image_digest(),
        limits=_limits(),
        drain_timeout_seconds=0.01,
    )
    run_id = UUID(int=24)
    operation = asyncio.create_task(manager.for_run("tenant-a", run_id, 1).write("late", b"x"))
    await adapter.started.wait()
    release = asyncio.create_task(manager.release_run(run_id))
    await asyncio.sleep(0.02)
    assert release.done() is False
    adapter.proceed.set()
    with pytest.raises(ExecutionRejected, match="released"):
        await operation
    await release
    assert adapter.live_environment_ids() == frozenset()
    assert manager._locks == {}
    assert manager._lock_users == {}


async def test_sandbox_close_reconciles_provisioning_after_drain_timeout() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    adapter = _BlockingProvisionEnvironment(clock, SequenceIdFactory())
    manager = SandboxManager(
        adapter,
        image_digest=fake_image_digest(),
        limits=_limits(),
        drain_timeout_seconds=0.01,
    )
    operation = asyncio.create_task(
        manager.for_run("tenant-a", UUID(int=25), 1).write("late", b"x")
    )
    await adapter.started.wait()
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0.02)
    assert closing.done() is False
    adapter.proceed.set()
    with pytest.raises(ExecutionRejected, match="closing"):
        await operation
    await closing
    assert adapter.live_environment_ids() == frozenset()
    assert manager._locks == {}
    assert manager._lock_users == {}
