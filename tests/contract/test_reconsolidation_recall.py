"""M32 recall contracts against the in-memory composition."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_history_cases import HISTORY_SCENARIOS
from tests.contract.reconsolidation_recall_cases import RECALL_SCENARIOS
from tests.contract.reconsolidation_summary_history_cases import SUMMARY_HISTORY_SCENARIOS
from tests.contract.reconsolidation_summary_recall_cases import SUMMARY_RECALL_SCENARIOS
from tests.contract.support import memory_uow_factory


async def test_summary_snapshot_erasure() -> None:
    from tests.contract.reconsolidation_summary_context_cases import summary_snapshot_erasure

    _, factory = await memory_uow_factory()
    await summary_snapshot_erasure(factory)


@pytest.mark.parametrize(
    "scenario", [*RECALL_SCENARIOS, *SUMMARY_RECALL_SCENARIOS], ids=lambda case: case.__name__
)
async def test_merge_recall(scenario: Callable[[UnitOfWorkFactory], Awaitable[None]]) -> None:
    _, factory = await memory_uow_factory()
    async with asyncio.timeout(5):
        await scenario(factory)


async def test_people_lock_reentry_does_not_admit_child_tasks() -> None:
    from tests.contract.support import principal

    _, factory = await memory_uow_factory()
    waiting = asyncio.Event()
    entered = asyncio.Event()

    async def child() -> None:
        waiting.set()
        async with factory() as other, other.people.lock(principal()):
            entered.set()

    async with asyncio.timeout(5):
        async with factory() as uow, uow.people.lock(principal()), uow.people.lock(principal()):
            task = asyncio.create_task(child())
            await waiting.wait()
            await asyncio.sleep(0.01)
            assert not entered.is_set()
        await task
        assert entered.is_set()


async def test_merge_citations_credit_only_the_rendered_original_and_refuse_ambiguity() -> None:
    from uuid import UUID

    from agent_core.adapters.determinism import RandomIdFactory
    from agent_core.memory.formation import GovernedMemoryService
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from tests.contract.memory_fixtures import memory, recall_query
    from tests.contract.reconsolidation_cases import Stores
    from tests.contract.reconsolidation_source_cases import seed
    from tests.contract.support import NOW, SESSION_ID, principal

    clock, factory = await memory_uow_factory()
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        newer = await uow.memories.get(memory(belief_id=502).id, principal())
        newer = newer.model_copy(update={"utility": 1})
        await uow.memories.reinforce(newer)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
        original = await uow.memories.get(plan.canonical_id, principal())
    enabled = HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    service = GovernedMemoryService(factory, clock, RandomIdFactory(), principal())
    await enabled.recall(recall_query(), session_id=SESSION_ID, turn_id=UUID(int=991))
    feedback = await service.record_usage(
        session_id=SESSION_ID, run_id=UUID(int=991), final_text="[m:00000000]"
    )
    assert feedback.cited == 1
    async with factory() as uow:
        used = await uow.memories.get(original.id, principal())
        assert used.utility > original.utility
        assert (
            used.model_copy(
                update={
                    "utility": original.utility,
                    "last_used_at": original.last_used_at,
                    "updated_at": original.updated_at,
                }
            )
            == original
        )
        assert await uow.memories.get(newer.id, principal()) == newer
        assert await uow.reconsolidation.get_merge(principal(), operation.id, NOW) == operation
    disabled = HybridMemoryRetriever(factory, clock, RandomIdFactory(), principal())
    await enabled.recall(recall_query(), session_id=SESSION_ID, turn_id=UUID(int=992))
    await disabled.recall(recall_query(), session_id=SESSION_ID, turn_id=UUID(int=992))
    ambiguous = await service.record_usage(
        session_id=SESSION_ID, run_id=UUID(int=992), final_text="[m:00000000]"
    )
    assert ambiguous.ambiguous == 1 and ambiguous.cited == ambiguous.uncited == 0
    async with factory() as uow:
        assert await uow.memories.get(original.id, principal()) == used
        assert await uow.memories.get(newer.id, principal()) == newer


@pytest.mark.parametrize(
    "scenario", [*HISTORY_SCENARIOS, *SUMMARY_HISTORY_SCENARIOS], ids=lambda case: case.__name__
)
async def test_historical_merge_recall(
    scenario: Callable[[UnitOfWorkFactory, FixedClock], Awaitable[None]],
) -> None:
    clock, factory = await memory_uow_factory()
    async with asyncio.timeout(5):
        await scenario(factory, clock)


async def test_connection_recall_is_explicit_and_revocation_restores_atoms() -> None:
    from typing import Any

    from agent_core.adapters.determinism import RandomIdFactory
    from agent_core.memory.reconsolidation_apply import apply_review
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from tests.contract.memory_fixtures import recall_query
    from tests.contract.reconsolidation_apply_cases import prepared_batch
    from tests.contract.support import SESSION_ID, principal

    clock, factory = await memory_uow_factory()

    def connection(ops: list[dict[str, Any]]) -> None:
        del ops[1:]
        ops[0]["clauses"][0]["text"] = "User may prefer concise explanations."

    prepared, review = await prepared_batch(
        cast(Factory, factory), kind="infer_connection", independent=True, change=connection
    )
    await apply_review(factory, clock, principal(), prepared, review, admitted=lambda: True)
    active = True
    retriever = HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=lambda: active
    )
    result = await retriever.recall(
        recall_query().model_copy(update={"min_score": 0, "max_items": 20, "budget_tokens": 2000}),
        session_id=SESSION_ID,
    )
    assert any(item.record_kind == "hypothesis" for item in result.items)
    assert "(hypothesis; inferred, low)" in result.rendered
    active = False
    ordinary = await retriever.recall(recall_query(), session_id=SESSION_ID)
    assert all(item.record_kind == "belief" for item in ordinary.items)
