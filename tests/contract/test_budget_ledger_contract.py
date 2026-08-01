import pytest

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.messages import ModelUsage
from agent_core.domain.runs import BudgetScope, Step
from agent_core.runtime.budgets import InMemoryBudgetLedger
from tests.contract.support import NOW, memory_stack, run


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
