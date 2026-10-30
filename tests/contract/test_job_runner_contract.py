"""ADR-0268: the execution port runs code without an implicit model turn."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.adapters.execution.jobs import SandboxJobRunner
from agent_core.domain.execution import EgressPolicy, ExecutionResult, ResourceLimits
from agent_core.domain.jobs import JobProgram, JobResult
from agent_core.execution.manager import SandboxManager
from agent_core.ports.jobs import JobRunner

RUN = UUID(int=300)
LIMITS = ResourceLimits(1000, 128 * 1024 * 1024, 32, 1024 * 1024, 100, 60)


async def test_job_result_is_returned_without_a_model_call_and_its_sandbox_is_released() -> None:
    environment = FakeExecutionEnvironment(
        FixedClock(datetime(2026, 10, 29, tzinfo=UTC)), SequenceIdFactory()
    )
    environment.queue_result(
        ExecutionResult(
            0, b'{"ok":true,"value":{"count":3}}', b"", False, False, False, None, (), 1
        )
    )
    manager = SandboxManager(environment, image_digest=fake_image_digest(), limits=LIMITS)
    runner: JobRunner = SandboxJobRunner(manager)

    async def no_broker(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        raise AssertionError("A code-only job must not dispatch a model or other broker call.")

    program = JobProgram("def main(inputs, job):\n    return {'count': len(inputs['items'])}\n")
    result = await runner.run(
        program,
        {"items": [1, 2, 3]},
        tenant_id="owner",
        run_id=RUN,
        lease_epoch=2,
        dispatch=no_broker,
    )

    assert result == JobResult(value={"count": 3}, diagnostics="", broker_calls=0)
    [spec] = environment.specifications
    assert spec.egress == EgressPolicy()
    assert spec.run_id == RUN and spec.lease_epoch == 2
    [(_, command)] = environment.commands
    assert tuple(command.argv) == ("python", "-m", "agent_core.execution.job_runner")
    assert json.loads(command.stdin or b"") == {
        "source": program.source,
        "inputs": {"items": [1, 2, 3]},
    }
    assert environment.live_environment_ids() == frozenset()


async def test_declared_calls_reach_the_supplied_broker_without_an_implicit_model_turn() -> None:
    from agent_core.domain.execution import BridgeEndpoint, EnvironmentHandle, ExecutionCommand
    from agent_core.tools.bridge import ProgrammaticBridgeSession

    seen: list[str] = []
    replies: list[object] = []

    class CallingEnvironment(FakeExecutionEnvironment):
        async def execute_with_bridge(
            self,
            environment: EnvironmentHandle,
            command: ExecutionCommand,
            endpoint: BridgeEndpoint,
            handler: object,
        ) -> ExecutionResult:
            assert isinstance(handler, ProgrammaticBridgeSession)
            for ordinal, name in enumerate(
                ("http.request", "model.complete", "workspace.read_text")
            ):
                replies.append(
                    json.loads(
                        await handler.handle(
                            json.dumps(
                                {
                                    "token": endpoint.token,
                                    "call": name,
                                    "arguments": {},
                                    "ordinal": ordinal,
                                }
                            ).encode()
                        )
                    )
                )
            return await self.execute(environment, command)

    environment = CallingEnvironment(
        FixedClock(datetime(2026, 10, 29, tzinfo=UTC)), SequenceIdFactory()
    )
    environment.queue_result(
        ExecutionResult(0, b'{"ok":true,"value":"done"}', b"", False, False, False, None, (), 1)
    )
    manager = SandboxManager(environment, image_digest=fake_image_digest(), limits=LIMITS)
    runner: JobRunner = SandboxJobRunner(manager)

    async def broker(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        assert call_id.startswith("bridge:")
        seen.append(call)
        return {"status": "succeeded", "result": {"text": "Summary"}}

    result = await runner.run(
        JobProgram(
            "def main(inputs, job): return 'done'", frozenset({"http.request", "model.complete"})
        ),
        {},
        tenant_id="owner",
        run_id=RUN,
        lease_epoch=1,
        dispatch=broker,
    )
    assert seen == ["http.request", "model.complete"]
    assert replies[-1] == {
        "status": "denied",
        "reason_code": "job.undeclared_call",
        "retryable": False,
    }
    assert result.broker_calls == 3
    assert environment.live_environment_ids() == frozenset()


async def test_a_job_failure_is_repair_feedback_and_releases_its_sandbox() -> None:
    import pytest

    from agent_core.domain.jobs import JobExecutionError

    environment = FakeExecutionEnvironment(
        FixedClock(datetime(2026, 10, 29, tzinfo=UTC)), SequenceIdFactory()
    )
    environment.queue_result(
        ExecutionResult(
            1,
            b'{"ok":false,"error":"ValueError: Missing reviews"}',
            b"",
            False,
            False,
            False,
            None,
            (),
            1,
        )
    )
    runner = SandboxJobRunner(
        SandboxManager(environment, image_digest=fake_image_digest(), limits=LIMITS)
    )

    async def no_broker(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        raise AssertionError("No broker request was expected.")

    with pytest.raises(JobExecutionError, match="Missing reviews"):
        await runner.run(
            JobProgram("def main(inputs, job): raise ValueError('Missing reviews')"),
            {},
            tenant_id="owner",
            run_id=RUN,
            lease_epoch=1,
            dispatch=no_broker,
        )
    assert environment.live_environment_ids() == frozenset()


async def test_cancelling_execution_releases_the_job_sandbox() -> None:
    import asyncio

    import pytest

    from agent_core.domain.execution import EnvironmentHandle, ExecutionCommand

    class CancelledEnvironment(FakeExecutionEnvironment):
        async def execute(
            self, environment: EnvironmentHandle, command: ExecutionCommand
        ) -> ExecutionResult:
            raise asyncio.CancelledError

    environment = CancelledEnvironment(
        FixedClock(datetime(2026, 10, 29, tzinfo=UTC)), SequenceIdFactory()
    )
    runner = SandboxJobRunner(
        SandboxManager(environment, image_digest=fake_image_digest(), limits=LIMITS)
    )

    async def no_broker(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        raise AssertionError("No broker request was expected.")

    with pytest.raises(asyncio.CancelledError):
        await runner.run(
            JobProgram("def main(inputs, job): return 1"),
            {},
            tenant_id="owner",
            run_id=RUN,
            lease_epoch=1,
            dispatch=no_broker,
        )
    assert environment.live_environment_ids() == frozenset()


async def test_catching_the_bridge_call_limit_cannot_turn_a_failed_job_into_success() -> None:
    from typing import Any

    import pytest

    from agent_core.domain.jobs import JobExecutionError
    from agent_core.tools.bridge import ProgrammaticBridgeSession

    class ExceedingEnvironment(FakeExecutionEnvironment):
        async def execute_with_bridge(
            self, environment: Any, command: Any, endpoint: Any, handler: Any
        ) -> ExecutionResult:
            assert isinstance(handler, ProgrammaticBridgeSession)
            for ordinal in range(3):
                await handler.handle(
                    json.dumps(
                        {
                            "token": endpoint.token,
                            "call": "log",
                            "arguments": {"message": "Hello"},
                            "ordinal": ordinal,
                        }
                    ).encode()
                )
            return ExecutionResult(
                0, b'{"ok":true,"value":1}', b"", False, False, False, None, (), 1
            )

    environment = ExceedingEnvironment(
        FixedClock(datetime(2026, 10, 29, tzinfo=UTC)), SequenceIdFactory()
    )
    runner = SandboxJobRunner(
        SandboxManager(environment, image_digest=fake_image_digest(), limits=LIMITS),
        maximum_calls=2,
    )

    async def dispatch(*args: Any) -> dict[str, Any]:
        return {"status": "succeeded", "result": {}}

    with pytest.raises(JobExecutionError, match="limit"):
        await runner.run(
            JobProgram("def main(inputs, job): return 1", frozenset({"log"})),
            {},
            tenant_id="owner",
            run_id=RUN,
            lease_epoch=1,
            dispatch=dispatch,
        )
