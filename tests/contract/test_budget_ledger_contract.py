from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.messages import ModelUsage
from agent_core.domain.runs import BudgetScope, Run, RunLimits, Step
from agent_core.runtime.budgets import InMemoryBudgetLedger
from tests.contract.support import NOW, RUN_ID, memory_stack, principal, run


async def test_budget_ledger_records_usage_and_stops_before_excess() -> None:
    clock, _sessions, runs, _events = await memory_stack()
    current = run(max_steps=1)
    await runs.create(current)
    ledger = InMemoryBudgetLedger(runs, clock)
    ledger.check(current, BudgetScope.STEP)
    current.step_count = 1
    with pytest.raises(BudgetExceededError):
        ledger.check(current, BudgetScope.STEP)
    current.step_count = 0
    step = Step(run_id=current.id, step_number=1, started_at=NOW)
    await ledger.record_model_usage(current, ModelUsage(input_tokens=3), step=step)
    assert current.model_call_count == 1
    assert current.usage.input_tokens == 3


def _limited(**limits: Any) -> Run:
    """A run at a chosen point against explicit limits (runtime-loop.md, Budget)."""

    deadline = limits.pop("deadline_at", None)
    counters = {
        key: limits.pop(key)
        for key in ("step_count", "model_call_count", "tool_call_count")
        if key in limits
    }
    usage = limits.pop("usage", None)
    current = run().model_copy(
        update={
            "limits": RunLimits(
                max_steps=4, max_model_calls=4, max_tool_calls=4, deadline_at=deadline, **limits
            ),
            "deadline_at": deadline,
            **counters,
        }
    )
    if usage is not None:
        current.usage = current.usage.model_copy(update=usage)
    return current


# Each row: the run's state, and the scopes that must refuse the next operation.
# ADMISSION enforces deadline and cost; STEP adds max_steps; ATTEMPT adds the
# model-call and token limits; TOOL_CALL adds max_tool_calls. STEP never
# enforces max_tool_calls (ADR-0115): a run at its tool-call limit still owes
# a tool-free answer.
_ALL = frozenset(BudgetScope)
SCOPE_MATRIX: list[tuple[str, dict[str, Any], frozenset[BudgetScope], str]] = [
    ("fresh", {}, frozenset(), ""),
    ("deadline_elapsed", {"deadline_at": NOW}, _ALL, "deadline_exceeded"),
    (
        "cost_limit_reached",
        {"max_cost": Decimal("1.00"), "usage": {"cost": Decimal("1.00")}},
        _ALL,
        "budget_exceeded",
    ),
    ("step_limit_reached", {"step_count": 4}, frozenset({BudgetScope.STEP}), "max_steps_exceeded"),
    (
        "model_call_limit_reached",
        {"model_call_count": 4},
        frozenset({BudgetScope.ATTEMPT}),
        "budget_exceeded",
    ),
    (
        "input_token_limit_reached",
        {"max_input_tokens": 100, "usage": {"input_tokens": 100}},
        frozenset({BudgetScope.ATTEMPT}),
        "budget_exceeded",
    ),
    (
        "output_token_limit_reached",
        {"max_output_tokens": 50, "usage": {"output_tokens": 50}},
        frozenset({BudgetScope.ATTEMPT}),
        "budget_exceeded",
    ),
    (
        "tool_call_limit_reached",
        {"tool_call_count": 4},
        frozenset({BudgetScope.TOOL_CALL}),
        "budget_exceeded",
    ),
    (
        "one_below_every_limit",
        {
            "step_count": 3,
            "model_call_count": 3,
            "tool_call_count": 3,
            "max_input_tokens": 100,
            "max_output_tokens": 50,
            "max_cost": Decimal("1.00"),
            "deadline_at": NOW + timedelta(microseconds=1),
            "usage": {"input_tokens": 99, "output_tokens": 49, "cost": Decimal("0.99")},
        },
        frozenset(),
        "",
    ),
]


@pytest.mark.parametrize(
    ("state", "refusing", "reason"),
    [
        pytest.param(state, refusing, reason, id=name)
        for name, state, refusing, reason in SCOPE_MATRIX
    ],
)
@pytest.mark.parametrize("scope", list(BudgetScope), ids=lambda scope: scope.value)
async def test_each_scope_checks_exactly_its_own_limits_before_the_operation(
    scope: BudgetScope,
    state: dict[str, Any],
    refusing: frozenset[BudgetScope],
    reason: str,
) -> None:
    clock, _sessions, runs, _events = await memory_stack()
    ledger = InMemoryBudgetLedger(runs, clock)
    current = _limited(**dict(state))

    if scope in refusing:
        with pytest.raises(BudgetExceededError) as refused:
            ledger.check(current, scope)
        assert refused.value.reason == reason
    else:
        ledger.check(current, scope)


@pytest.mark.parametrize(
    ("limits", "usage", "tools"),
    [
        pytest.param({"max_input_tokens": 10}, ModelUsage(input_tokens=11), 0, id="input_tokens"),
        pytest.param(
            {"max_output_tokens": 10}, ModelUsage(output_tokens=11), 0, id="output_tokens"
        ),
        pytest.param({"max_cost": Decimal("0.10")}, ModelUsage(cost=Decimal("0.11")), 0, id="cost"),
        pytest.param({}, ModelUsage(), 5, id="tool_calls"),
    ],
)
async def test_recording_past_a_limit_persists_the_totals_then_fails_the_run(
    limits: dict[str, Any], usage: ModelUsage, tools: int
) -> None:
    """The "after" check is the record: totals are stored, then BudgetExceeded is raised."""

    clock, _sessions, runs, _events = await memory_stack()
    current = _limited(**limits)
    await runs.create(current)
    ledger = InMemoryBudgetLedger(runs, clock)
    step = Step(run_id=current.id, step_number=1, started_at=NOW)

    with pytest.raises(BudgetExceededError) as exceeded:
        if tools:
            await ledger.record_tool_usage(current, tools, step=step)
        else:
            await ledger.record_model_usage(current, usage, step=step)

    assert exceeded.value.reason == "budget_exceeded"
    stored = await runs.get(RUN_ID, principal())
    assert stored.usage == current.usage
    assert stored.tool_call_count == current.tool_call_count == tools
    assert stored.model_call_count == current.model_call_count == (0 if tools else 1)
    assert stored.usage.input_tokens == usage.input_tokens
    assert stored.usage.output_tokens == usage.output_tokens
    assert stored.usage.cost == usage.cost


async def test_recording_exactly_at_a_limit_is_allowed() -> None:
    clock, _sessions, runs, _events = await memory_stack()
    current = _limited(max_input_tokens=10, max_cost=Decimal("0.10"), model_call_count=3)
    await runs.create(current)
    ledger = InMemoryBudgetLedger(runs, clock)
    step = Step(run_id=current.id, step_number=1, started_at=NOW)

    await ledger.record_model_usage(
        current, ModelUsage(input_tokens=10, cost=Decimal("0.10")), step=step
    )
    await ledger.record_tool_usage(current, 4, step=step)

    assert (current.model_call_count, current.tool_call_count) == (4, 4)
    with pytest.raises(BudgetExceededError):
        ledger.check(current, BudgetScope.ATTEMPT)


async def test_usage_accumulates_across_attempts_and_reasoning_stays_unknown_until_reported() -> (
    None
):
    clock, _sessions, runs, _events = await memory_stack()
    current = run()
    await runs.create(current)
    ledger = InMemoryBudgetLedger(runs, clock)
    step = Step(run_id=current.id, step_number=1, started_at=NOW)

    await ledger.record_model_usage(
        current,
        ModelUsage(
            input_tokens=10,
            cached_input_tokens=4,
            cache_write_input_tokens=2,
            output_tokens=3,
            cost=Decimal("0.01"),
        ),
        step=step,
    )
    assert current.usage.reasoning_tokens is None
    await ledger.record_model_usage(
        current,
        ModelUsage(input_tokens=5, cached_input_tokens=1, output_tokens=2, reasoning_tokens=7),
        step=step,
    )
    await ledger.record_model_usage(current, ModelUsage(reasoning_tokens=None), step=step)
    await ledger.record_tool_usage(current, 2, step=step)

    usage = current.usage
    assert (usage.input_tokens, usage.cached_input_tokens, usage.cache_write_input_tokens) == (
        15,
        5,
        2,
    )
    assert (usage.output_tokens, usage.reasoning_tokens) == (5, 7)
    assert (usage.model_calls, current.model_call_count) == (3, 3)
    assert (usage.tool_calls, current.tool_call_count) == (2, 2)
    assert usage.cost == Decimal("0.01")
    assert (await runs.get(RUN_ID, principal())).usage == usage


async def test_an_orchestration_turn_refund_frees_its_step_and_model_call() -> None:
    """The refunded totals are what the next step's check sees, and never go negative."""

    clock, _sessions, runs, _events = await memory_stack()
    current = _limited(step_count=4, model_call_count=4)
    await runs.create(current)
    ledger = InMemoryBudgetLedger(runs, clock)
    step = Step(run_id=current.id, step_number=4, started_at=NOW)
    with pytest.raises(BudgetExceededError):
        ledger.check(current, BudgetScope.STEP)

    await ledger.refund_orchestration_turn(current, step=step)

    ledger.check(current, BudgetScope.STEP)
    ledger.check(current, BudgetScope.ATTEMPT)
    stored = await runs.get(RUN_ID, principal())
    assert (stored.step_count, stored.model_call_count) == (3, 3)

    unspent = current.model_copy(update={"step_count": 0, "model_call_count": 0})
    await ledger.refund_orchestration_turn(unspent, step=step)
    assert (unspent.step_count, unspent.model_call_count) == (0, 0)
    stored = await runs.get(RUN_ID, principal())
    assert (stored.step_count, stored.model_call_count) == (0, 0)
