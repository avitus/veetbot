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

from agent_core.adapters.determinism import SystemClock
from agent_core.application.public_services import _run_view
from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.delegations import derive_child_limits
from agent_core.domain.errors import DelegationValidationError
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
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.runtime import loop as loop_module
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.runtime.loop import meter_bridge, step_number_at
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import memory_uow_factory, principal
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
                # As the real bridge does, an identity names its script too.
                await dispatch(name, {}, f"bridge-{self.spec.name}-{execution}-{index}")
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

    meter_bridge(run, step, [cast(Any, object())] * batch)

    assert step.bridge_call_allowance == room


# --- a paused or interrupted step ---------------------------------------------

# A tool the default profile asks about before it runs.
_WRITE = _spec(
    "demo.write",
    side_effect=SideEffectClass.EXTERNAL_WRITE,
    risk=RiskLevel.HIGH,
    idempotency=IdempotencyClass.NON_IDEMPOTENT,
)


async def test_a_scripts_calls_are_charged_when_its_batch_pauses_for_an_approval(
    tmp_path: Path,
) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    write = _Probe(_WRITE)
    script = _Script(_RUN_CODE, [["demo.ordinary"] * 3])

    run = await _run(
        tmp_path,
        [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"), ("demo.write", "write"))],
        [script, ordinary, write],
        approvals=1,
    )

    # The script ran once, before the pause. On resume its recorded result is
    # returned, and the three calls it made are still charged exactly once.
    assert (len(script.steps), ordinary.executions, write.executions) == (1, 3, 1)
    assert run.tool_call_count == 2 + 3
    assert run.usage.tool_calls == 2 + 3
    assert run.usage.unsettled_bridged_calls == []


def _paused(*, counted: int, unsettled: int, limit: int = 24) -> Run:
    """A run inside a step: some calls counted, some made over the bridge and not yet."""
    run = Run(
        id=UUID(int=0xA12),
        session_id=UUID(int=0x5E56),
        tenant_id=principal().tenant_id,
        agent_id=UUID(int=0xA6),
        agent_version="agent-v1",
        status=RunStatus.WAITING_FOR_APPROVAL,
        limits=RunLimits(max_tool_calls=limit),
        tool_call_count=counted,
        created_at=_NOW,
        updated_at=_NOW,
    )
    run.usage = run.usage.model_copy(
        update={"unsettled_bridged_calls": [f"bridge:{index}" for index in range(unsettled)]}
    )
    return run


def test_a_run_inside_a_step_reports_the_calls_its_scripts_have_made() -> None:
    # However the run ends from here, what it shows is what it did.
    assert _run_view(_paused(counted=2, unsettled=3)).tool_call_count == 5


def test_a_child_run_gets_no_room_the_parents_scripts_already_used() -> None:
    from tests.gates.test_delegate_m13 import DEFAULTS, _brief

    parent = _paused(counted=1, unsettled=3, limit=4).model_copy(
        update={"status": RunStatus.RUNNING}
    )

    with pytest.raises(DelegationValidationError, match="tool calls"):
        derive_child_limits(parent, [_brief(None)], DEFAULTS, now=_NOW)


async def test_calls_made_before_a_pause_keep_their_place_in_the_limit(tmp_path: Path) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(_RUN_CODE, [["demo.ordinary"] * 2])
    # Asks first, then makes two calls of its own once it is approved.
    command = _Script(_OTHER_SANDBOX_TOOL, [["demo.ordinary"] * 2])

    run = await _run(
        tmp_path,
        [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"), ("demo.script", "command"))],
        [script, command, ordinary],
        limits=RunLimits(max_tool_calls=5),
        approvals=1,
    )

    # Five calls of room: the batch's two and three over the bridge. The
    # script used two before the pause, so the command has room for one.
    refusal = {"status": "failed", "reason_code": "tool.budget_exhausted", "retryable": False}
    assert [response["status"] for response in command.responses[0]] == ["succeeded", "failed"]
    assert command.responses[0][1] == refusal
    assert ordinary.executions == 3
    assert run.tool_call_count == 5


class _Repeating(_Script):
    """A script that makes each of its calls twice under the same identity."""

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments
        self.steps.append(context.step_number)
        dispatch = cast(Any, context.bridge_dispatch)
        self.responses.append(
            [await dispatch(name, {}, "bridge-same") for name in self._plans[0] * 2]
        )
        return ToolResult(
            ok=True,
            content=[TextPart(text="script finished")],
            structured={},
            output_trust=self.spec.output_trust,
        )


async def test_a_call_a_script_repeats_under_one_identity_is_charged_once(
    tmp_path: Path,
) -> None:
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Repeating(_RUN_CODE, [["demo.ordinary"]])

    run = await _run(tmp_path, [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))], [script, ordinary])

    # A re-executed script repeats its earlier calls. Each returns its
    # recorded outcome and is not a new call.
    assert [response["status"] for response in script.responses[0]] == ["succeeded"] * 2
    assert ordinary.executions == 1
    assert run.tool_call_count == 1 + 1


class _Changing(_Script):
    """A script run again after a stop that asks for something else at the same position."""

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments
        self.steps.append(context.step_number)
        dispatch = cast(Any, context.bridge_dispatch)
        self.responses.append(
            [
                await dispatch("demo.numbered", {"n": 1}, "bridge-position-0"),
                await dispatch("demo.numbered", {"n": 1}, "bridge-position-0"),
                await dispatch("demo.numbered", {"n": 2}, "bridge-position-0"),
                await dispatch("demo.ordinary", {}, "bridge-position-0"),
            ]
        )
        return ToolResult(
            ok=True,
            content=[TextPart(text="script finished")],
            structured={},
            output_trust=self.spec.output_trust,
        )


async def test_a_different_call_at_a_position_already_charged_is_a_new_call(
    tmp_path: Path,
) -> None:
    numbered = _Probe(
        _spec(
            "demo.numbered",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "additionalProperties": False,
            },
        )
    )
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Changing(_RUN_CODE, [[]])

    run = await _run(
        tmp_path, [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))], [script, numbered, ordinary]
    )

    # The exact repeat returned its recorded outcome. The call with other
    # arguments and the call to another tool each ran, so each is charged.
    assert (numbered.executions, ordinary.executions) == (2, 1)
    assert run.tool_call_count == 1 + 3


async def test_calls_whose_charge_was_interrupted_are_charged_before_the_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop between the checkpoint that ends a step and the ledger write."""
    ordinary = _Probe(_spec("demo.ordinary"))
    script = _Script(_RUN_CODE, [["demo.ordinary"] * 3])
    settle = loop_module.settle_bridged_calls
    skipped: list[int] = []

    async def interrupted(context: Any, step: Step, *, refund: bool) -> None:
        if not skipped:
            skipped.append(step.step_number)
            return
        await settle(context, step, refund=refund)

    monkeypatch.setattr(loop_module, "settle_bridged_calls", interrupted)

    run = await _run(tmp_path, [_turn((SANDBOX_RUN_CODE_TOOL_NAME, "script"))], [script, ordinary])

    assert skipped == [1]
    assert run.tool_call_count == 1 + 3
    assert run.usage.unsettled_bridged_calls == []


async def test_a_recovered_checkpoint_keeps_the_run_s_refunded_steps() -> None:
    """A checkpoint rebuilt from the run row numbers the next step as the lost one did."""
    _fixed, uow_factory = await memory_uow_factory()
    owner = principal()
    session_id = UUID(int=0x5E55)
    refunded = Run(
        id=UUID(int=0xA11),
        session_id=session_id,
        tenant_id=owner.tenant_id,
        agent_id=UUID(int=0xA6),
        agent_version="agent-v1",
        status=RunStatus.RUNNING,
        step_count=1,
        created_at=_NOW,
        updated_at=_NOW,
    )
    refunded.usage = refunded.usage.model_copy(update={"refunded_steps": 2})
    async with uow_factory() as uow:
        await uow.sessions.create(
            Session(
                id=session_id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                agent_id=UUID(int=0xA6),
                agent_version="agent-v1",
                status=SessionStatus.ACTIVE,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await uow.runs.create(refunded)
        seeded = await DurableCheckpointSeeder(SystemClock())(uow, refunded, None, None, owner)

    # Two steps were given back, so the run's second counted step is its fourth.
    assert step_number_at(refunded.step_count + 1, seeded) == 4
