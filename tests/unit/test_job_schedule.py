"""Job schedules pin code and grants and can reserve zero model spend."""

from decimal import Decimal
from uuid import UUID

import pytest

from agent_core.domain.runs import BudgetScope, RunKind, RunLimits
from agent_core.domain.schedules import AgentRunTask, JobTask, ScheduleRevision
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from tests.contract.support import memory_uow_factory, run
from tests.contract.test_schedule_repository_contract import revision


def test_existing_schedule_documents_default_to_agent_runs() -> None:
    old = revision().model_dump(mode="json")
    old.pop("task", None)
    assert isinstance(ScheduleRevision.model_validate(old).task, AgentRunTask)


def test_code_only_schedule_can_pin_a_revision_with_zero_model_budget() -> None:
    values = revision().model_dump()
    values["task"] = JobTask(slug="count-items", revision_id=UUID(int=1), grant_id=UUID(int=2))
    values["limits"] = RunLimits(
        max_steps=1, max_model_calls=0, max_tool_calls=64, max_cost=Decimal(0)
    )
    saved = ScheduleRevision.model_validate(values)
    assert isinstance(saved.task, JobTask)
    assert saved.task.revision_id == UUID(int=1)
    assert saved.limits.max_cost == 0
    values["task"] = AgentRunTask()
    with pytest.raises(ValueError, match="positive"):
        ScheduleRevision.model_validate(values)


async def test_zero_cost_job_admits_computation_but_refuses_model_attempts() -> None:
    clock, factory = await memory_uow_factory()
    current = run().model_copy(
        update={"kind": RunKind.JOB, "limits": RunLimits(max_model_calls=0, max_cost=Decimal(0))}
    )
    ledger = UnitOfWorkBudgetLedger(factory, clock)
    ledger.check(current, BudgetScope.ADMISSION)
    from agent_core.domain.errors import BudgetExceededError

    with pytest.raises(BudgetExceededError):
        ledger.check(current, BudgetScope.ATTEMPT)
