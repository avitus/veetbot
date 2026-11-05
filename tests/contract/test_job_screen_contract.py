"""The job screen uses an isolated builder and never runs job source."""

import json

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.adapters.jobs.screen import SandboxJobScreen
from agent_core.domain.execution import ExecutionResult
from agent_core.domain.jobs import JobBrokerError
from agent_core.execution.manager import SandboxManager
from agent_core.ports.cockpit import BuildScreen
from agent_core.ports.jobs import JobScreen
from tests.contract.support import NOW, principal
from tests.contract.test_job_runner_contract import LIMITS


@pytest.mark.parametrize("reject", [False, True])
async def test_screen_returns_the_builder_verdict_and_releases_its_sandbox(reject: bool) -> None:
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    diagnostics = (
        [{"rule": "screen.name", "file": "main.py", "message": "Contains a known name."}]
        if reject
        else []
    )
    environment.queue_result(
        ExecutionResult(
            int(reject),
            json.dumps({"ok": not reject, "diagnostics": diagnostics}).encode(),
            b"",
            False,
            False,
            False,
            None,
            (),
            1,
        )
    )
    screen: JobScreen = SandboxJobScreen(
        SandboxManager(environment, image_digest=fake_image_digest(), limits=LIMITS),
        SequenceIdFactory(),
    )
    files = {"main.py": "raise RuntimeError('must not execute')", "job.json": "{}"}
    result = await screen.check(principal(), files, screen=BuildScreen(names=("Test Owner",)))
    assert bool(result) is reject
    [(_, command)] = environment.commands
    assert command.argv == ("node", "/opt/cockpit/builder/job-screen.mjs")
    assert json.loads(command.stdin or b"")["files"] == files
    assert not environment.live_environment_ids()


async def test_invalid_screen_output_refuses_publication_and_releases_the_sandbox() -> None:
    environment = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    environment.queue_result(ExecutionResult(0, b"{}", b"", False, False, False, None, (), 1))
    screen = SandboxJobScreen(
        SandboxManager(environment, image_digest=fake_image_digest(), limits=LIMITS),
        SequenceIdFactory(),
    )
    with pytest.raises(JobBrokerError, match="screen_unavailable"):
        await screen.check(principal(), {"main.py": "pass", "job.json": "{}"}, screen=BuildScreen())
    assert not environment.live_environment_ids()
