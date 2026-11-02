"""ADR-0281: code.test's sandbox, its report taken on trust no further than checked."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agent_core.adapters.code.sandbox import (
    CODE_CHECK_ARGV,
    CODE_CHECK_TIMEOUT_SECONDS,
    SandboxCodeChecker,
    code_check_sandbox,
)
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.docker import DockerExecutionEnvironment
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.domain.code import CodeCheck, CodeCheckKind, CodeCheckResult
from agent_core.domain.errors import ExecutionUnavailable
from agent_core.domain.execution import (
    EgressPolicy,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    KillReason,
    ResourceLimits,
    SandboxPurpose,
)
from agent_core.ports.code import CodeChecker, CodeCheckUnavailableError

NOW = datetime(2026, 11, 1, tzinfo=UTC)
RUN = UUID(int=280)
LIMITS = ResourceLimits(2000, 2 * 1024**3, 512, 4 * 1024**3, 100_000, 300)
CHECK = CodeCheck(kind=CodeCheckKind.PYTEST, targets=("tests/unit/test_x.py",), keyword="test_y")
FILES: dict[str, str | None] = {"src/agent_core/x.py": "VALUE = 2\n", "src/old.py": None}


def _report(**overrides: object) -> bytes:
    report: dict[str, object] = {
        "passed": True,
        "exit_code": 0,
        "timed_out": False,
        "output": "1 passed",
        "seconds": 1.5,
    }
    report.update(overrides)
    return json.dumps(report).encode()


def _result(
    stdout: bytes,
    exit_code: int | None = 0,
    *,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    timed_out: bool = False,
    killed_by: KillReason | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        exit_code, stdout, b"", stdout_truncated, stderr_truncated, timed_out, killed_by, (), 10
    )


def _harness(
    *results: ExecutionResult,
) -> tuple[FakeExecutionEnvironment, SandboxCodeChecker]:
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    for result in results:
        environment.queue_result(result)
    manager = code_check_sandbox(environment, image_digest=fake_image_digest(), limits=LIMITS)
    return environment, SandboxCodeChecker(manager, SequenceIdFactory())


async def _run(checker: CodeChecker) -> CodeCheckResult:
    return await checker.run(tenant_id="owner", run_id=RUN, lease_epoch=3, files=FILES, check=CHECK)


async def test_a_check_runs_in_a_fresh_offline_sandbox_with_a_closed_command_and_is_released() -> (
    None
):
    environment, checker = _harness(_result(_report()))

    result = await _run(checker)

    assert result == CodeCheckResult(
        passed=True, exit_code=0, timed_out=False, output="1 passed", seconds=1.5
    )
    [spec] = environment.specifications
    assert spec.egress == EgressPolicy()
    assert spec.purpose is SandboxPurpose.CODE_CHECK
    assert spec.tenant_id == "owner"
    assert "SYNTHETIC" not in json.dumps(dict(spec.environment))
    assert set(spec.environment) == {"HOME", "PWD", "PATH", "TMPDIR", "LANG"}
    [(_, command)] = environment.commands
    assert tuple(command.argv) == CODE_CHECK_ARGV
    assert command.timeout_seconds == CODE_CHECK_TIMEOUT_SECONDS == 300
    assert json.loads(command.stdin or b"") == {
        "files": FILES,
        "check": {"kind": "pytest", "targets": ["tests/unit/test_x.py"], "keyword": "test_y"},
    }
    assert environment.live_environment_ids() == frozenset()


async def test_the_sandbox_takes_no_environment_from_the_parent_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_DATABASE_URL", "synthetic-must-not-pass")
    environment, checker = _harness(_result(_report()))
    await _run(checker)
    [spec] = environment.specifications
    assert "AGENT_DATABASE_URL" not in spec.environment
    assert "synthetic-must-not-pass" not in spec.environment.values()


async def test_two_checks_in_one_run_lease_each_get_their_own_sandbox() -> None:
    environment, checker = _harness(_result(_report()), _result(_report()))
    await _run(checker)
    await _run(checker)
    first, second = environment.specifications
    assert first.run_id != second.run_id
    assert environment.live_environment_ids() == frozenset()


async def test_a_failing_check_is_a_result_not_an_error() -> None:
    _, checker = _harness(
        _result(_report(passed=False, exit_code=1, output="1 failed", seconds=2), 1)
    )
    result = await _run(checker)
    assert result.passed is False
    assert result.exit_code == 1
    assert result.output == "1 failed"


async def test_a_refused_target_is_a_result_without_an_exit_code() -> None:
    _, checker = _harness(
        _result(_report(passed=False, exit_code=None, output="Refused target", seconds=0), 1)
    )
    result = await _run(checker)
    assert result.passed is False
    assert result.exit_code is None


async def test_a_check_that_timed_out_inside_is_reported_and_never_a_pass() -> None:
    _, checker = _harness(
        _result(_report(passed=False, exit_code=None, timed_out=True, output="slow"), 1)
    )
    result = await _run(checker)
    assert result.passed is False
    assert result.timed_out is True
    assert result.output == "slow"


async def test_the_output_is_clipped_to_its_tail_whatever_the_report_claims() -> None:
    _, checker = _harness(_result(_report(output="a" * 7000 + "tail")))
    result = await _run(checker)
    assert len(result.output) == 6000
    assert result.output.endswith("tail")


@pytest.mark.parametrize(
    "execution",
    [
        _result(_report(), None, timed_out=True, killed_by=KillReason.TIMEOUT),
        _result(_report(), None, killed_by=KillReason.MEMORY),
        _result(_report(), None, killed_by=KillReason.DISK),
        _result(_report(), None, killed_by=KillReason.PIDS),
        _result(_report(), 0, stdout_truncated=True),
        _result(_report(), 0, stderr_truncated=True),
        _result(b"", 0),
        _result(b"not json", 0),
        _result(b"[]", 0),
        _result(b'{"passed": true}', 0),
        # A pass the exit code denies, and the reverse.
        _result(_report(), 1),
        _result(_report(passed=False, exit_code=1), 0),
        # A report that contradicts itself.
        _result(_report(exit_code=1), 0),
        _result(_report(exit_code=None), 0),
        _result(_report(timed_out=True), 0),
        # Near misses that a lax parser would accept as a pass.
        _result(_report(passed="true"), 0),
        _result(_report(passed=1), 0),
        _result(_report(exit_code=False), 0),
        _result(_report(exit_code="0"), 0),
        _result(_report(timed_out="false"), 0),
        _result(_report(output=7), 0),
        _result(_report(seconds="1"), 0),
        _result(_report(extra="field"), 0),
        # An exit status the entry point never uses.
        _result(_report(passed=False, exit_code=1), 2),
    ],
)
async def test_a_run_past_its_limits_or_with_a_malformed_report_is_an_error_and_released(
    execution: ExecutionResult,
) -> None:
    environment, checker = _harness(execution)
    with pytest.raises(CodeCheckUnavailableError):
        await _run(checker)
    assert environment.live_environment_ids() == frozenset()


async def test_an_unreachable_execution_service_is_the_same_error() -> None:
    class Unreachable(FakeExecutionEnvironment):
        async def provision(self, specification: EnvironmentSpec) -> EnvironmentHandle:
            raise ExecutionUnavailable("execution service is unreachable")

    manager = code_check_sandbox(
        Unreachable(FixedClock(NOW), SequenceIdFactory()),
        image_digest=fake_image_digest(),
        limits=LIMITS,
    )
    with pytest.raises(CodeCheckUnavailableError):
        await _run(SandboxCodeChecker(manager, SequenceIdFactory()))


async def test_cancelling_a_check_releases_its_sandbox() -> None:
    class Cancelled(FakeExecutionEnvironment):
        async def execute(
            self, environment: EnvironmentHandle, command: ExecutionCommand
        ) -> ExecutionResult:
            raise asyncio.CancelledError

    environment = Cancelled(FixedClock(NOW), SequenceIdFactory())
    manager = code_check_sandbox(environment, image_digest=fake_image_digest(), limits=LIMITS)
    with pytest.raises(asyncio.CancelledError):
        await _run(SandboxCodeChecker(manager, SequenceIdFactory()))
    assert environment.live_environment_ids() == frozenset()


async def test_the_sandbox_always_allows_the_whole_check_timeout() -> None:
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    environment.queue_result(_result(_report()))
    manager = code_check_sandbox(
        environment,
        image_digest=fake_image_digest(),
        limits=replace(LIMITS, wall_clock_seconds=30),
    )
    await _run(SandboxCodeChecker(manager, SequenceIdFactory()))
    [spec] = environment.specifications
    assert spec.limits.wall_clock_seconds == CODE_CHECK_TIMEOUT_SECONDS


def test_the_timeout_must_be_positive() -> None:
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    manager = code_check_sandbox(environment, image_digest=fake_image_digest(), limits=LIMITS)
    with pytest.raises(ValueError, match="positive"):
        SandboxCodeChecker(manager, SequenceIdFactory(), timeout_seconds=0)


def _listing(check: UUID, *, created: datetime, expires: datetime) -> bytes:
    return (
        f"container-{check.int}\tenvironment-{check.int}\t{check}\t0\t"
        f"{int(expires.timestamp())}\t{int(created.timestamp())}\tcode_check\n"
    ).encode()


async def test_no_run_lease_holds_a_check_so_only_its_own_deadline_reaps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(NOW)
    check = UUID(int=281)
    listing = _listing(check, created=NOW, expires=NOW + timedelta(minutes=5))
    operations: list[tuple[str, ...]] = []

    async def docker(*arguments: str, stdin: bytes | None = None) -> bytes:
        del stdin
        operations.append(arguments)
        return listing if arguments[0] == "ps" else b""

    monkeypatch.setattr("agent_core.adapters.execution.docker._docker", docker)
    adapter = DockerExecutionEnvironment(clock, SequenceIdFactory(), reaper_grace_seconds=60)

    async def no_run_holds_it(_run_id: UUID, _lease_epoch: int) -> bool:
        return False

    clock.advance(timedelta(seconds=120))
    assert await adapter.reap(frozenset(), no_run_holds_it) == 0
    clock.advance(timedelta(seconds=181))
    assert await adapter.reap(frozenset(), no_run_holds_it) == 1


async def test_the_fake_environment_also_holds_a_check_to_its_own_deadline() -> None:
    clock = FixedClock(NOW)
    environment = FakeExecutionEnvironment(clock, SequenceIdFactory())
    await environment.provision(
        EnvironmentSpec(
            tenant_id="owner",
            run_id=UUID(int=282),
            lease_epoch=0,
            image_digest=fake_image_digest(),
            limits=LIMITS,
            egress=EgressPolicy(),
            environment={},
            purpose=SandboxPurpose.CODE_CHECK,
        )
    )
    assert await environment.reap(frozenset()) == 0
    clock.advance(timedelta(seconds=301))
    assert await environment.reap(frozenset()) == 1
