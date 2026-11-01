"""ADR-0281: the public-repository screen runs offline and fails closed."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent_core.adapters.cockpit.builder import cockpit_build_sandbox
from agent_core.adapters.code.screen import CODE_SCREEN_ARGV, SandboxCodeScreen
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.domain.code import CodeFinding
from agent_core.domain.execution import (
    EgressPolicy,
    EnvironmentHandle,
    ExecutionCommand,
    ExecutionResult,
    KillReason,
    ResourceLimits,
    SandboxPurpose,
)
from agent_core.ports.code import CodeScreen, CodeScreenUnavailableError

RUN = UUID(int=283)
LIMITS = ResourceLimits(1000, 256 * 1024 * 1024, 64, 4 * 1024 * 1024, 1000, 60)
FINDING = {"file": "src/x.py", "code": "screen.secret", "message": "src/x.py contains a secret"}


def _result(
    stdout: bytes,
    exit_code: int | None,
    *,
    stdout_truncated: bool = False,
    timed_out: bool = False,
    killed_by: KillReason | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        exit_code, stdout, b"", stdout_truncated, False, timed_out, killed_by, (), 1
    )


def _harness(result: ExecutionResult) -> tuple[FakeExecutionEnvironment, SandboxCodeScreen]:
    environment = FakeExecutionEnvironment(
        FixedClock(datetime(2026, 11, 1, tzinfo=UTC)), SequenceIdFactory()
    )
    environment.queue_result(result)
    manager = cockpit_build_sandbox(environment, image_digest=fake_image_digest(), limits=LIMITS)
    return environment, SandboxCodeScreen(manager, SequenceIdFactory())


async def _screen(screen: CodeScreen) -> tuple[CodeFinding, ...]:
    return await screen.screen(
        tenant_id="owner",
        run_id=RUN,
        lease_epoch=1,
        added_lines={"src/x.py": "VALUE = 1\n"},
        commit_message="Fix the value",
        names=("Synthetic Name",),
        settings_values=("synthetic-setting",),
    )


async def test_clean_text_has_no_findings_and_the_builder_sandbox_is_released() -> None:
    environment, screen = _harness(_result(b'{"ok":true,"findings":[]}', 0))

    assert await _screen(screen) == ()

    [spec] = environment.specifications
    assert spec.egress == EgressPolicy()
    assert spec.purpose is SandboxPurpose.COCKPIT_BUILD
    assert spec.tenant_id == "owner"
    [(_, command)] = environment.commands
    assert tuple(command.argv) == CODE_SCREEN_ARGV
    assert json.loads(command.stdin or b"") == {
        "files": {"src/x.py": "VALUE = 1\n"},
        "screen": {
            "names": ["Synthetic Name"],
            "settingsValues": ["synthetic-setting"],
            "commitMessage": "Fix the value",
        },
    }
    assert environment.live_environment_ids() == frozenset()


async def test_a_finding_is_returned_as_the_screen_named_it() -> None:
    _, screen = _harness(_result(json.dumps({"ok": False, "findings": [FINDING]}).encode(), 1))
    assert await _screen(screen) == (CodeFinding(**FINDING),)


async def test_screening_twice_in_one_run_lease_uses_two_sandboxes() -> None:
    environment, screen = _harness(_result(b'{"ok":true,"findings":[]}', 0))
    environment.queue_result(_result(b'{"ok":true,"findings":[]}', 0))
    await _screen(screen)
    await _screen(screen)
    first, second = environment.specifications
    assert first.run_id != second.run_id


@pytest.mark.parametrize(
    "execution",
    [
        _result(b'{"ok":true,"findings":[]}', None, timed_out=True, killed_by=KillReason.TIMEOUT),
        _result(b'{"ok":true,"findings":[]}', None, killed_by=KillReason.MEMORY),
        _result(b'{"ok":true,"findings":[]}', 0, stdout_truncated=True),
        # The screen could not read its input, which is not a clean result.
        _result(b'{"ok":false,"findings":[]}', 2),
        _result(b"", 0),
        _result(b"not json", 0),
        _result(b"[]", 0),
        _result(b'{"ok":true}', 0),
        # A verdict that disagrees with the exit code or with its own findings.
        _result(b'{"ok":true,"findings":[]}', 1),
        _result(b'{"ok":false,"findings":[]}', 1),
        _result(json.dumps({"ok": False, "findings": [FINDING]}).encode(), 0),
        _result(json.dumps({"ok": True, "findings": [FINDING]}).encode(), 0),
        _result(b'{"ok":"true","findings":[]}', 0),
        _result(b'{"ok":false,"findings":[{"file":"x"}]}', 1),
    ],
)
async def test_a_screen_that_did_not_finish_cleanly_fails_closed_and_is_released(
    execution: ExecutionResult,
) -> None:
    environment, screen = _harness(execution)
    with pytest.raises(CodeScreenUnavailableError):
        await _screen(screen)
    assert environment.live_environment_ids() == frozenset()


async def test_cancelling_the_screen_releases_its_sandbox() -> None:
    class Cancelled(FakeExecutionEnvironment):
        async def execute(
            self, environment: EnvironmentHandle, command: ExecutionCommand
        ) -> ExecutionResult:
            raise asyncio.CancelledError

    environment = Cancelled(FixedClock(datetime(2026, 11, 1, tzinfo=UTC)), SequenceIdFactory())
    manager = cockpit_build_sandbox(environment, image_digest=fake_image_digest(), limits=LIMITS)
    with pytest.raises(asyncio.CancelledError):
        await _screen(SandboxCodeScreen(manager, SequenceIdFactory()))
    assert environment.live_environment_ids() == frozenset()
