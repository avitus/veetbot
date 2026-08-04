"""Shared execution-environment semantics exercised against the deterministic adapter."""

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
    await manager.workspace_for("tenant-a", run_id, 1)
    await manager.workspace_for("tenant-a", run_id, 2)
    with pytest.raises(ExceptionGroup):
        await manager.release_run(run_id)
    assert len(adapter.destroy_attempts) == 2
    assert len(adapter.live_environment_ids()) == 1
    await manager.release_run(run_id)
    assert adapter.live_environment_ids() == frozenset()
