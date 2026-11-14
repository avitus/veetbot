"""Follow-up inbox contract shared by memory and PostgreSQL (ADR-0324)."""

from collections.abc import Awaitable, Callable
from uuid import uuid5

import pytest

from agent_core.domain.agents import Principal
from agent_core.domain.messages import TextPart
from agent_core.domain.pending_inputs import PendingRunInput
from agent_core.domain.runs import Run
from agent_core.ports.repositories import RunPendingInputRepository
from tests.contract.support import NOW, memory_uow_factory, principal, run

Scenario = Callable[[RunPendingInputRepository, Principal, Run], Awaitable[None]]
SCENARIOS: list[Scenario] = []


def scenario(function: Scenario) -> Scenario:
    SCENARIOS.append(function)
    return function


def pending(
    owner: Principal, active: Run, number: int, text: str = "also check prices"
) -> PendingRunInput:
    return PendingRunInput(
        id=uuid5(active.id, str(number)),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=active.session_id,
        run_id=active.id,
        content=[TextPart(text=text)],
        created_at=NOW,
    )


@scenario
async def follow_ups_are_listed_in_arrival_order(
    inbox: RunPendingInputRepository, owner: Principal, active: Run
) -> None:
    # Identifiers and timestamps deliberately disagree with arrival order.
    first = pending(owner, active, 9, "first")
    second = pending(owner, active, 3, "second")
    assert await inbox.add(first) == first
    assert await inbox.add(second) == second
    assert await inbox.list_for_run(active.id, owner) == [first, second]
    assert await inbox.get(second.id, owner) == second


@scenario
async def a_deleted_follow_up_is_gone_and_a_second_delete_reports_it(
    inbox: RunPendingInputRepository, owner: Principal, active: Run
) -> None:
    first = await inbox.add(pending(owner, active, 1))
    kept = await inbox.add(pending(owner, active, 2, "kept"))
    assert await inbox.delete(first.id, owner) is True
    assert await inbox.delete(first.id, owner) is False
    assert await inbox.get(first.id, owner) is None
    assert await inbox.list_for_run(active.id, owner) == [kept]


@scenario
async def another_tenant_neither_reads_nor_deletes(
    inbox: RunPendingInputRepository, owner: Principal, active: Run
) -> None:
    saved = await inbox.add(pending(owner, active, 1))
    stranger = owner.model_copy(update={"tenant_id": f"{owner.tenant_id}-other"})
    assert await inbox.list_for_run(active.id, stranger) == []
    assert await inbox.delete(saved.id, stranger) is False
    assert await inbox.get(saved.id, stranger) is None
    assert await inbox.list_for_run(active.id, owner) == [saved]


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda function: function.__name__)
async def test_memory_run_pending_input_repository(check: Scenario) -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        active = run()
        await uow.runs.create(active)
        await check(uow.pending_inputs, principal(), active)
