"""Cockpit builds run in their own sandbox image and survive the run reaper (Milestone 35).

docs/plan/cockpit.md, Validation, the privacy screen and the build: a build
started outside a run is bounded by its own hard deadline rather than a run
lease, and the execution service admits only the images it was configured with.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.docker import DockerExecutionEnvironment
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.adapters.execution.service import ExecutionServiceServer, _decode, _encode
from agent_core.domain.errors import ExecutionRejected
from agent_core.domain.execution import (
    EgressPolicy,
    EnvironmentSpec,
    ResourceLimits,
    SandboxPurpose,
)

NOW = datetime(2026, 10, 23, tzinfo=UTC)
LIMITS = ResourceLimits(
    cpu_millicores=500,
    memory_bytes=128 * 1024 * 1024,
    pids_max=64,
    workspace_bytes=2 * 1024 * 1024,
    inodes_max=1000,
    wall_clock_seconds=120,
)


def _ps_line(run_id: UUID, *, purpose: str, created: datetime, expires: datetime) -> bytes:
    return (
        f"container-{run_id.int}\tenvironment-{run_id.int}\t{run_id}\t0\t"
        f"{int(expires.timestamp())}\t{int(created.timestamp())}\t{purpose}\n"
    ).encode()


def _spec(purpose: SandboxPurpose, image_digest: str) -> EnvironmentSpec:
    return EnvironmentSpec(
        tenant_id="tenant-a",
        run_id=UUID(int=35),
        lease_epoch=0,
        image_digest=image_digest,
        limits=LIMITS,
        egress=EgressPolicy(),
        environment={},
        purpose=purpose,
    )


def _reaper(
    monkeypatch: pytest.MonkeyPatch, clock: FixedClock, listing: bytes
) -> tuple[DockerExecutionEnvironment, list[tuple[str, ...]]]:
    operations: list[tuple[str, ...]] = []

    async def docker(*arguments: str, stdin: bytes | None = None) -> bytes:
        del stdin
        operations.append(arguments)
        return listing if arguments[0] == "ps" else b""

    monkeypatch.setattr("agent_core.adapters.execution.docker._docker", docker)
    adapter = DockerExecutionEnvironment(clock, SequenceIdFactory(), reaper_grace_seconds=60)
    return adapter, operations


def _removed(operations: list[tuple[str, ...]]) -> list[str]:
    return [operation[2] for operation in operations if operation[:2] == ("rm", "--force")]


async def test_a_build_outside_a_run_is_reaped_only_at_its_own_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(NOW)
    build = UUID(int=301)
    listing = _ps_line(
        build, purpose="cockpit_build", created=NOW, expires=NOW + timedelta(minutes=2)
    )
    adapter, operations = _reaper(monkeypatch, clock, listing)

    async def no_run_holds_it(_run_id: UUID, _lease_epoch: int) -> bool:
        return False

    clock.advance(timedelta(seconds=90))
    assert await adapter.reap(frozenset(), no_run_holds_it) == 0
    assert _removed(operations) == []
    clock.advance(timedelta(seconds=31))
    assert await adapter.reap(frozenset(), no_run_holds_it) == 1
    assert f"container-{build.int}" in _removed(operations)


async def test_a_run_sandbox_without_a_lease_is_still_reaped_after_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = UUID(int=302)
    for purpose in ("run", ""):
        listing = _ps_line(run, purpose=purpose, created=NOW, expires=NOW + timedelta(minutes=5))
        adapter, operations = _reaper(monkeypatch, FixedClock(NOW + timedelta(seconds=61)), listing)
        assert await adapter.reap(frozenset()) == 1, purpose
        assert f"container-{run.int}" in _removed(operations)


async def test_provisioning_labels_each_sandbox_with_its_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[tuple[str, ...]] = []

    async def docker(*arguments: str, stdin: bytes | None = None) -> bytes:
        del stdin
        operations.append(arguments)
        if arguments[0] == "create":
            return b"container-id\n"
        return b"{}" if arguments[0] == "exec" else b""

    monkeypatch.setattr("agent_core.adapters.execution.docker._docker", docker)
    adapter = DockerExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    await adapter.provision(_spec(SandboxPurpose.COCKPIT_BUILD, "sha256:" + "b" * 64))
    created = next(operation for operation in operations if operation[0] == "create")
    assert "agent.core.purpose=cockpit_build" in created
    assert "--read-only" in created
    assert "agent.core.purpose=run" not in created
    assert ps_format_reads_purpose(adapter)


def ps_format_reads_purpose(adapter: DockerExecutionEnvironment) -> bool:
    del adapter
    source = Path(__file__).resolve().parents[2] / "src/agent_core/adapters/execution/docker.py"
    return '{{.Label "agent.core.purpose"}}' in source.read_text(encoding="utf-8")


def test_the_purpose_crosses_the_execution_service_protocol() -> None:
    spec = _spec(SandboxPurpose.COCKPIT_BUILD, fake_image_digest())
    assert _decode(_encode(spec)) == spec
    legacy = EnvironmentSpec(
        "tenant-a", UUID(int=36), 0, fake_image_digest(), LIMITS, EgressPolicy(), {}
    )
    assert legacy.purpose is SandboxPurpose.RUN


async def test_the_execution_service_admits_only_its_configured_images(tmp_path: Path) -> None:
    digests = {
        "agent-core-sandbox:production": "sha256:" + "a" * 64,
        "veetbot-cockpit-builder:production": "sha256:" + "b" * 64,
        "attacker:latest": "sha256:" + "c" * 64,
    }

    async def resolve(reference: str) -> str:
        return digests[reference]

    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    server = ExecutionServiceServer(
        environment,
        tmp_path / "execution.sock",
        resolve_image_digest=resolve,
        allowed_images=("agent-core-sandbox:production", "veetbot-cockpit-builder:production"),
    )
    for reference in ("agent-core-sandbox:production", "veetbot-cockpit-builder:production"):
        spec = _spec(SandboxPurpose.RUN, digests[reference])
        await server.admit(spec)
    with pytest.raises(ExecutionRejected, match="not an allowed sandbox image"):
        await server.admit(_spec(SandboxPurpose.RUN, digests["attacker:latest"]))
    unrestricted = ExecutionServiceServer(
        environment, tmp_path / "other.sock", resolve_image_digest=resolve
    )
    await unrestricted.admit(_spec(SandboxPurpose.RUN, digests["attacker:latest"]))


async def test_an_allowed_image_that_does_not_exist_yet_blocks_no_other(tmp_path: Path) -> None:
    # The first release installs the allowlist before `:production` names a builder.
    async def resolve(reference: str) -> str:
        if reference == "veetbot-cockpit-builder:production":
            raise ExecutionRejected("no such image")
        return "sha256:" + "a" * 64

    server = ExecutionServiceServer(
        FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory()),
        tmp_path / "execution.sock",
        resolve_image_digest=resolve,
        allowed_images=("veetbot-cockpit-builder:production", "agent-core-sandbox:production"),
    )
    await server.admit(_spec(SandboxPurpose.RUN, "sha256:" + "a" * 64))
    with pytest.raises(ExecutionRejected, match="not an allowed sandbox image"):
        await server.admit(_spec(SandboxPurpose.COCKPIT_BUILD, "sha256:" + "b" * 64))
