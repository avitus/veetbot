"""Actual execution cannot renew a durable reservation or escape its scope."""

import asyncio
from datetime import timedelta

import pytest

from agent_core.adapters.determinism import FixedClock, elapsed_time
from agent_core.domain.recall_execution import (
    RecallBudgetExceededError,
    RecallExecutionPermit,
    recall_phase,
    recall_search,
)
from tests.contract.memory_fixtures import recall_query
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal


def permit(slots: int = 1, seconds: float = 10) -> RecallExecutionPermit:
    owner = principal()
    return RecallExecutionPermit(
        owner.tenant_id,
        owner.principal_id,
        SESSION_ID,
        RUN_ID,
        slots,
        NOW + timedelta(seconds=seconds),
        FixedClock(NOW).now,
        elapsed_time,
    )


async def search() -> None:
    async with recall_search(recall_query(), session_id=SESSION_ID, run_id=RUN_ID):
        await asyncio.sleep(0)


async def test_parallel_searches_cannot_share_one_slot_but_nested_people_read_can() -> None:
    allowance = permit()
    with recall_phase(allowance):
        async with recall_search(recall_query(), session_id=SESSION_ID, run_id=RUN_ID):
            await search()  # Same task's People -> belief implementation.
            with pytest.raises(RecallBudgetExceededError):
                await asyncio.create_task(search())
    assert allowance.remaining == 0

    allowance = permit(2)
    with recall_phase(allowance):
        outcomes = await asyncio.gather(*(search() for _ in range(5)), return_exceptions=True)
    assert sum(value is None for value in outcomes) == 2
    assert sum(isinstance(value, RecallBudgetExceededError) for value in outcomes) == 3


@pytest.mark.parametrize("field,value", [("tenant_id", "other"), ("principal_id", "other")])
async def test_scope_mismatch_cannot_borrow_an_admitted_search(field: str, value: str) -> None:
    allowance = permit()
    with recall_phase(allowance), pytest.raises(RecallBudgetExceededError):
        async with recall_search(
            recall_query().model_copy(update={field: value}),
            session_id=SESSION_ID,
            run_id=RUN_ID,
        ):
            pytest.fail("A different owner entered the permitted search")
    assert allowance.remaining == 1


async def test_phase_exit_revokes_permit_in_an_inherited_context() -> None:
    ready = asyncio.Event()

    async def later() -> None:
        await ready.wait()
        await search()

    allowance = permit()
    with recall_phase(allowance):
        task = asyncio.create_task(later())
    ready.set()
    with pytest.raises(RecallBudgetExceededError):
        await task
    assert allowance.remaining == 1


async def test_elapsed_deadline_applies_even_when_injected_clock_is_fixed() -> None:
    allowance = permit(seconds=0.01)
    with recall_phase(allowance), pytest.raises(RecallBudgetExceededError):
        async with recall_search(recall_query(), session_id=SESSION_ID, run_id=RUN_ID):
            await asyncio.sleep(1)
    assert allowance.remaining == 0


async def test_cancellation_consumes_attempt_without_becoming_ordinary_failure() -> None:
    allowance = permit()
    with recall_phase(allowance), pytest.raises(asyncio.CancelledError):
        async with recall_search(recall_query(), session_id=SESSION_ID, run_id=RUN_ID):
            raise asyncio.CancelledError
    assert allowance.remaining == 0 and allowance.closed


async def test_independent_runtime_phases_do_not_leak_task_local_allowances() -> None:
    first, second = permit(), permit()

    async def isolated(allowance: RecallExecutionPermit) -> None:
        with recall_phase(allowance):
            await search()

    await asyncio.gather(isolated(first), isolated(second))
    assert first.remaining == second.remaining == 0
