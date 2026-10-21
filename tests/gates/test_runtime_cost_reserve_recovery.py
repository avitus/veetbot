"""Cost-boundary answers retain their accounting and no-effect guarantee on recovery."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ModelUsage, ScriptedTurn, StopReason
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.runtime import loop
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _calc, _requests, _settings


class WorkerCrash(BaseException):
    """Bypass ordinary exception handling just like a lost worker process."""


@pytest.mark.parametrize("crash_at", ["model_response", "tool_call", "synthesis_response"])
@pytest.mark.parametrize(("max_steps", "reserve_steps"), [(32, 0), (3, 1)])
async def test_cost_reserve_recovery_never_executes_refused_calls(
    monkeypatch: pytest.MonkeyPatch, crash_at: str, max_steps: int, reserve_steps: int
) -> None:
    """Resume refusals without effects, including a concurrent step-reserve boundary."""

    original_checkpoint = loop.checkpoint
    original_usage = UnitOfWorkBudgetLedger.record_model_usage
    interrupted: UUID | None = None

    async def checkpoint(context: Any, trigger: str) -> None:
        """Interrupt the selected step before its checkpoint becomes durable."""

        nonlocal interrupted
        if context.run.step_count == 2 and trigger == crash_at:
            interrupted = context.run.id
            raise WorkerCrash
        await original_checkpoint(context, trigger)

    async def record_usage(self: Any, run: Any, *args: Any, **kwargs: Any) -> None:
        """Crash after synthesis usage is durable but before violation handling."""

        nonlocal interrupted
        await original_usage(self, run, *args, **kwargs)
        if crash_at == "synthesis_response" and run.step_count == 3:
            interrupted = run.id
            raise WorkerCrash

    turns = [
        ScriptedTurn(
            tool_calls=[_calc("1+1", "performed")],
            stop_reason=StopReason.TOOL_USE,
            usage=ModelUsage(cost=Decimal("2")),
        ),
        ScriptedTurn(
            tool_calls=[_calc("2+2", "refused")],
            stop_reason=StopReason.TOOL_USE,
            usage=ModelUsage(cost=Decimal("6")),
        ),
        ScriptedTurn(tool_calls=[_calc("3+3", "violation")], stop_reason=StopReason.TOOL_USE)
        if crash_at == "synthesis_response"
        else ScriptedTurn(text="Two. Further research was not performed."),
    ]
    async with build(
        settings=_settings(),
        script=FakeModelScript(turns=turns),
        clock=FixedClock(NOW),
        limits=RunLimits(
            max_steps=max_steps,
            synthesis_reserve_steps=reserve_steps,
            max_cost=Decimal("10"),
            synthesis_reserve_cost=Decimal("3"),
        ),
    ) as app:
        monkeypatch.setattr(loop, "checkpoint", checkpoint)
        monkeypatch.setattr(UnitOfWorkBudgetLedger, "record_model_usage", record_usage)
        with pytest.raises(WorkerCrash):
            await app.runs.submit("Research and report")
        assert interrupted is not None
        monkeypatch.setattr(loop, "checkpoint", original_checkpoint)
        monkeypatch.setattr(UnitOfWorkBudgetLedger, "record_model_usage", original_usage)
        async with app.uow_factory() as uow:
            await uow.runs.transition(interrupted, RunStatus.RUNNING, RunStatus.QUEUED)
        await app.executor.execute(interrupted)
        run = await app.runs.get(interrupted)
        events = await app.runs.events(interrupted)
        requests = _requests(app)
    if crash_at == "synthesis_response":
        assert run.status is RunStatus.FAILED
        assert run.failure is not None
        assert run.failure.details == {"synthesis_reserve": "steps" if reserve_steps else "cost"}
    else:
        assert run.status is RunStatus.COMPLETED, run.failure
    assert run.tool_call_count == 1
    assert run.model_call_count == 3
    assert requests[-1].tool_choice == "none"
    refusals = [event for event in events if event.event_type == "tool.call.failed"]
    assert len(refusals) == 1 and refusals[0].payload["call_id"] == "refused"
    assert sum(event.event_type == "tool.call.started" for event in events) == 1
