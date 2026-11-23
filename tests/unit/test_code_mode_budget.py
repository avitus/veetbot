"""Code mode budget: the step refund and the bridged tool-call count (ADR-0342).

An orchestration-only step gives back its step and its model call. Every
bridged call a script makes counts against the run's tool-call limit, and the
bridge refuses a call the limit has no room for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.execution import SANDBOX_RUN_CODE_TOOL_NAME
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
)
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.runs import Run, RunCheckpoint, RunLimits, RunStatus, Step
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.runtime.loop import meter_bridge, step_number_at
from agent_core.tools.registry import StaticToolRegistry
from tests.unit.test_code_mode import _Probe, _spec
from tests.unit.test_reply_attachments import _settings

# Two scripts in one run must differ, as two real scripts do, or the repeated-call
# guard ends the run before the second executes.
_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string"}},
    "additionalProperties": False,
}
# Stand-ins for the accounting alone. The real tool's classification and trust
# are covered in test_code_mode.py.
_RUN_CODE = _spec(
    SANDBOX_RUN_CODE_TOOL_NAME,
    input_schema=_SOURCE_SCHEMA,
    target_kind="sandbox",
    side_effect=SideEffectClass.CODE_EXECUTION,
)
_OTHER_SANDBOX_TOOL = _spec(
    "demo.script",
    target_kind="sandbox",
    side_effect=SideEffectClass.CODE_EXECUTION,
)


class _Script:
    """A sandbox-targeted tool; each execution makes the next plan's bridge calls."""

    def __init__(self, spec: ToolSpec, plans: list[list[str]]) -> None:
        self.spec = spec
        self._plans = plans
        self.steps: list[int] = []
        self.responses: list[list[dict[str, Any]]] = []

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments
        execution = len(self.steps)
        self.steps.append(context.step_number)
        dispatch = cast(Any, context.bridge_dispatch)
        self.responses.append(
            [
                await dispatch(name, {}, f"bridge-{execution}-{index}")
                for index, name in enumerate(self._plans[execution])
            ]
        )
        return ToolResult(
            ok=True,
            content=[TextPart(text="script finished")],
            structured={},
            output_trust=self.spec.output_trust,
        )


_NOW = datetime(2026, 11, 22, tzinfo=UTC)


def _source(call_id: str) -> str:
    return f"print('the {call_id} script')"


def _turn(*calls: tuple[str, str]) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name=name,
                arguments={"code": _source(call_id)} if name == SANDBOX_RUN_CODE_TOOL_NAME else {},
                call_id=call_id,
            )
            for name, call_id in calls
        ],
        stop_reason=StopReason.TOOL_USE,
    )


async def _run(
    tmp_path: Path,
    turns: list[ScriptedTurn],
    tools: list[Any],
    *,
    limits: RunLimits | None = None,
    approvals: int = 0,
) -> Run:
    """Run the scripted turns to the end, granting exactly `approvals` requests."""
    model = FakeModelScript(
        turns=[*turns, ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN)]
    )
    names = [tool.spec.name for tool in tools]
    async with build(
        settings=_settings(tmp_path),
        script=model,
        enabled_tools=names,
        limits=limits,
    ) as built:
        registry = cast(StaticToolRegistry, built.tool_pipeline._registry)
        for tool in tools:
            registry.register(tool)
        run_id = await built.runs.submit("Run these scripts.")
        for _ in range(approvals):
            waiting = await built.runs.get(run_id)
            assert waiting.status is RunStatus.WAITING_FOR_APPROVAL, waiting.failure
            [approval] = await built.approvals.list_pending(run_id=run_id)
            await built.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await built.runs.get(run_id)
    assert run.status is RunStatus.COMPLETED, run.failure
    return run


# --- the refund ---------------------------------------------------------------


async def test_an_orchestration_only_step_gives_back_its_step_and_model_call(
    tmp_path: Path,
) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(_RUN_CODE, [["demo.ordinary"] * 3])

    run = await _run(tmp_path, [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))], [script, ordinary])

    # Two model steps ran; the script's step is refunded and the answer's is not.
    assert (run.step_count, run.model_call_count) == (1, 1)
    # The refund frees budget. It does not rewrite what the run actually used.
    assert run.usage.model_calls == 2
    assert ordinary.executions == 3


async def test_a_script_that_calls_no_tool_keeps_its_step(tmp_path: Path) -> None:
    script = _Script(_RUN_CODE, [[]])

    run = await _run(tmp_path, [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))], [script])

    assert (run.step_count, run.model_call_count) == (2, 2)
    assert run.tool_call_count == 1


async def test_a_step_that_also_calls_another_tool_keeps_its_step(tmp_path: Path) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(_RUN_CODE, [["demo.ordinary"]])

    run = await _run(
        tmp_path,
        [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"), ("demo.ordinary", "direct"))],
        [script, ordinary],
    )

    assert (run.step_count, run.model_call_count) == (2, 2)
    assert run.tool_call_count == 3


async def test_another_sandbox_tool_is_never_refunded_but_its_bridged_calls_count(
    tmp_path: Path,
) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    command = _Script(_OTHER_SANDBOX_TOOL, [["demo.ordinary"] * 2])

    run = await _run(tmp_path, [_turn(("demo.script", "command"))], [command, ordinary])

    assert (run.step_count, run.model_call_count) == (2, 2)
    assert run.tool_call_count == 3


async def test_step_numbers_do_not_repeat_after_a_refund(tmp_path: Path) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(_RUN_CODE, [["demo.ordinary"], ["demo.ordinary"]])

    run = await _run(
        tmp_path,
        [
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "first")),
            _turn((SANDBOX_RUN_CODE_TOOL_NAME, "second")),
        ],
        [script, ordinary],
        # Policy asks before a second script once the first has returned output.
        approvals=1,
    )

    # Three model steps, two refunded. The second script still ran as step 2.
    assert (run.step_count, run.model_call_count) == (1, 1)
    assert script.steps == [1, 2]
    assert ordinary.executions == 2


def _checkpoint(budget_state: dict[str, Any]) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=UUID(int=1),
        version=1,
        status=RunStatus.RUNNING,
        budget_state=budget_state,
        created_at=_NOW,
    )


def test_a_step_number_adds_back_the_steps_the_checkpoint_records_as_refunded() -> None:
    assert step_number_at(3, _checkpoint({"refunded_steps": 2})) == 5


@pytest.mark.parametrize("recorded", [{}, {"refunded_steps": 0}, {"refunded_steps": "2"}])
def test_a_run_that_was_never_refunded_numbers_steps_by_its_step_count(
    recorded: dict[str, Any],
) -> None:
    assert step_number_at(3, _checkpoint(recorded)) == 3


async def test_a_script_approved_after_a_suspension_is_refunded_on_resume(
    tmp_path: Path,
) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(
        _spec(
            SANDBOX_RUN_CODE_TOOL_NAME,
            input_schema=_SOURCE_SCHEMA,
            target_kind="sandbox",
            side_effect=SideEffectClass.EXTERNAL_WRITE,
            risk=RiskLevel.HIGH,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
        ),
        [["demo.ordinary"] * 2],
    )

    run = await _run(
        tmp_path,
        [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))],
        [script, ordinary],
        approvals=1,
    )

    assert (run.step_count, run.model_call_count) == (1, 1)
    assert run.tool_call_count == 3
    assert ordinary.executions == 2


# --- the tool-call limit ------------------------------------------------------


async def test_bridged_calls_count_against_the_tool_call_limit(tmp_path: Path) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(_RUN_CODE, [["demo.ordinary"] * 3])

    run = await _run(tmp_path, [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))], [script, ordinary])

    # One call for the script and one for each call it made.
    assert run.tool_call_count == 4
    assert run.usage.tool_calls == 4


async def test_the_bridge_refuses_a_call_the_tool_call_limit_has_no_room_for(
    tmp_path: Path,
) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(_RUN_CODE, [["demo.ordinary"] * 4])

    run = await _run(
        tmp_path,
        [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))],
        [script, ordinary],
        limits=RunLimits(max_tool_calls=3),
    )

    refusal = {"status": "failed", "reason_code": "tool.budget_exhausted", "retryable": False}
    assert [response["status"] for response in script.responses[0][:2]] == ["succeeded"] * 2
    assert script.responses[0][2:] == [refusal, refusal]
    # The run ends with its answer, at the limit and not past it.
    assert run.tool_call_count == 3
    assert ordinary.executions == 2


@pytest.mark.parametrize(("used", "batch", "room"), [(0, 1, 23), (20, 4, 0), (24, 3, 0)])
def test_the_bridge_allowance_is_the_limit_less_the_count_and_the_batch(
    used: int, batch: int, room: int
) -> None:
    run = cast(Run, type("_Run", (), {"limits": RunLimits(), "tool_call_count": used})())
    step = Step(run_id=UUID(int=1), step_number=1, started_at=_NOW)
    step.bridged_tool_calls = 5

    meter_bridge(run, step, [cast(Any, object())] * batch)

    assert (step.bridged_tool_calls, step.bridge_call_allowance) == (0, room)
