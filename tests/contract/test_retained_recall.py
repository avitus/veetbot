"""Saved deliberate evidence is checked against its current authoritative support."""

from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.domain.memory import RecalledBelief
from agent_core.domain.messages import MemoryRecallUsage
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.memory.source_recovery import BoundedSourceRecovery
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import formation_stack, memory, recall_query, user_event
from tests.contract.support import RUN_ID, SESSION_ID, principal


@pytest.mark.parametrize("changed", [False, True])
async def test_retained_partial_source_package_validates_only_its_rendered_quotes(
    changed: bool,
) -> None:
    clock, factory, _, _ = await formation_stack()
    await retained_source_package(changed, clock, factory)


async def retained_source_package(
    changed: bool, clock: FixedClock, factory: UnitOfWorkFactory
) -> None:
    sequences = [await user_event(factory, "Workshop detail: " + str(i) * 700) for i in range(2)]
    record = memory(statement="The workshop is on Thursday.").model_copy(
        update={
            "subject": "workshop",
            "scope": "general",
            "source_event_ids": sequences,
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    admitted = [True]
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8000, 8010)),
        principal(),
        source_recovery=BoundedSourceRecovery(principal(), admitted=lambda: admitted[0]),
    )
    query = recall_query(text="workshop", budget_tokens=400).model_copy(
        update={"current_scope": "general"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID, run_id=RUN_ID)
    assert isinstance(result.items[0], RecalledBelief)
    assert len(result.items[0].sources) == 1, "This budget admits one of two whole quotes"
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    usage = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items),
        content_sha256=trace.rendered_sha256,
    )
    if changed:
        admitted[0] = False
    assert await retriever.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID) is (
        not changed
    )


@pytest.mark.parametrize("change", [None, "erasure", "admission"])
async def test_retained_semantic_summary_validates_support_without_rescoring_relevance(
    change: str | None,
) -> None:
    from agent_core.adapters.determinism import RandomIdFactory
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
    from tests.contract.reconsolidation_summary_recall_cases import summary_recall_inputs
    from tests.contract.semantic_memory_cases import SPACE, VECTOR
    from tests.contract.support import memory_uow_factory
    from tests.contract.test_semantic_recall import LocalQuery

    clock, factory = await memory_uow_factory()
    operation, originals = await summary_recall_inputs(factory)
    async with factory() as uow:
        for original in originals:
            source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
            assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
    admitted = [True]
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        RandomIdFactory(),
        principal(),
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(update={"semantic_enabled": True}),
        reconsolidation_enabled=lambda: admitted[0],
        semantic_queries=LocalQuery(),
    )
    query = recall_query(text="Alternative recommendations", max_items=4).model_copy(
        update={"candidate_selection": "relevance"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID, run_id=RUN_ID)
    assert operation.id in {item.belief_id for item in result.items}
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
        if change == "erasure":
            await uow.memories.fence_for_erasure(principal(), [originals[-1].id])
    if change == "admission":
        admitted[0] = False
    usage = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items),
        content_sha256=trace.rendered_sha256,
    )
    assert await retriever.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID) is (
        change is None
    )
