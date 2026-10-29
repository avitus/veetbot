"""Explicit completions use the ordinary model adapter and durable usage ledger."""

from decimal import Decimal

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.messages import (
    FakeModelScript,
    ModelPricing,
    ModelUsage,
    ResolvedModel,
    ScriptedTurn,
)
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from agent_core.runtime.job_model import JobModelCompletion
from tests.contract.support import NOW, memory_uow_factory, principal, run


async def test_explicit_model_call_has_no_tools_and_updates_the_same_run_and_ledger() -> None:
    clock, factory = await memory_uow_factory()
    current = run()
    async with factory() as uow:
        await uow.runs.create(current)
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text="Three reviews need attention.",
                    usage=ModelUsage(input_tokens=20, output_tokens=10, cost=Decimal("0.003")),
                )
            ]
        ),
        clock,
    )
    completion = JobModelCompletion(
        run=current,
        provider=provider,
        resolved=ResolvedModel(
            provider="fake",
            model="scripted",
            resolved_at=NOW,
            pricing=ModelPricing(input_per_mtok=Decimal("10"), output_per_mtok=Decimal("20")),
        ),
        budgets=UnitOfWorkBudgetLedger(factory, clock),
        clock=clock,
        ids=SequenceIdFactory(),
    )
    assert not provider.requests
    reply = await completion.complete(
        prompt="Summarize these reviews.",
        max_output_tokens=100,
        maximum_cost=Decimal("0.10"),
        call_id="summary",
    )
    assert reply == {"text": "Three reviews need attention.", "cost": "0.003"}
    [request] = provider.requests
    assert request.tools == [] and request.tool_choice == "none"
    assert request.maximum_output_tokens == 100
    assert request.maximum_provider_attempts == 1
    assert current.usage.cost == Decimal("0.003")
    assert current.model_call_count == 1
    async with factory() as uow:
        stored = await uow.runs.get(current.id, principal())
    assert stored.usage == current.usage


@pytest.mark.parametrize("maximum_cost", [Decimal("0"), Decimal("0.00001")])
async def test_unfunded_completion_is_refused_before_the_model_request(
    maximum_cost: Decimal,
) -> None:
    clock, factory = await memory_uow_factory()
    provider = FakeModelProvider(FakeModelScript(turns=[]), clock)
    completion = JobModelCompletion(
        run=run(),
        provider=provider,
        resolved=ResolvedModel(
            provider="fake",
            model="scripted",
            resolved_at=NOW,
            pricing=ModelPricing(input_per_mtok=Decimal("10"), output_per_mtok=Decimal("20")),
        ),
        budgets=UnitOfWorkBudgetLedger(factory, clock),
        clock=clock,
        ids=SequenceIdFactory(),
    )
    with pytest.raises(ValueError, match=r"job\.model_budget_exceeded"):
        await completion.complete(
            prompt="Summarize", max_output_tokens=100, maximum_cost=maximum_cost, call_id="summary"
        )
    assert provider.requests == []
