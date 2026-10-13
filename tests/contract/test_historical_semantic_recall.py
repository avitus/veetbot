"""Shared historical semantic behavior, exercised first in memory."""

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest

from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.historical_semantic_cases import (
    historical_attribution_revision_survives_later_text_change,
    historical_revision_is_not_current_evidence,
    historical_revision_restrictions_precede_limit,
    historical_selection_precedes_similarity,
    historical_visibility_and_stale_writes,
)
from tests.contract.support import memory_uow_factory


async def test_historical_revision_is_not_current_evidence() -> None:
    clock, factory = await memory_uow_factory()
    await historical_revision_is_not_current_evidence(factory, clock.advance)


async def test_historical_people_identity_precedes_candidate_limits() -> None:
    from tests.contract.semantic_memory_cases import people_identity_precedes_dense_limit

    _, factory = await memory_uow_factory()
    await people_identity_precedes_dense_limit(factory, historical=True)


@pytest.mark.parametrize(
    "scenario",
    [
        historical_attribution_revision_survives_later_text_change,
        historical_visibility_and_stale_writes,
        historical_selection_precedes_similarity,
        historical_revision_restrictions_precede_limit,
    ],
    ids=lambda case: case.__name__,
)
async def test_historical_boundaries(
    scenario: Callable[[UnitOfWorkFactory, Callable[[timedelta], None]], Awaitable[None]],
) -> None:
    clock, factory = await memory_uow_factory()
    await scenario(factory, clock.advance)


async def test_historical_local_worker_and_query_cache_are_bounded_and_explicit() -> None:
    from agent_core.memory.semantic_indexing import SemanticIndexingPass
    from agent_core.memory.semantic_queries import SemanticQueryPreparation
    from tests.contract.memory_fixtures import memory, recall_query
    from tests.contract.semantic_indexing_cases import INDEX_CONFIG, Provider
    from tests.contract.support import NOW, principal
    from tests.contract.test_local_semantic_preparation import CONFIG, LOCAL
    from tests.contract.test_semantic_query_preparation import CONTEXT

    clock, factory = await memory_uow_factory()
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory(statement="User avoids dairy"))
    clock.advance(timedelta(hours=1))
    async with factory() as uow:
        await uow.memories.reinforce(
            original.model_copy(update={"statement": "User now eats dairy", "store_position": 2})
        )
    config = CONFIG.model_copy(update={"historical_indexing": True})
    provider = Provider(factory)
    worker = SemanticIndexingPass(
        factory, clock, principal(), provider, config, admitted=lambda: True, local=LOCAL
    )
    result = await worker.run_slice()
    assert result.indexed == 3, result
    assert len(provider.calls) == 1 and len(provider.calls[0].texts) == 3
    assert (await worker.run_slice()).reason == "not_due"
    query = recall_query(text="Plan breakfast").model_copy(
        update={"candidate_selection": "relevance", "known_at": NOW, "as_of": NOW}
    )
    async with factory() as uow:
        hits = await uow.semantic_memory.search(query, LOCAL.space, (1.0, 0.0, 0.0), limit=1)
        assert [hit.record.statement for hit in hits] == [original.statement]
    lookup = SemanticQueryPreparation(
        factory,
        clock,
        principal(),
        provider,
        config,
        admitted=lambda: True,
        context=lambda: CONTEXT,
        local=LOCAL,
    )
    assert (await lookup.prepare([query])).prepared == 1
    assert lookup.lookup(query) is not None
    assert lookup.lookup(query.model_copy(update={"known_at": clock.now()})) is None
    assert lookup.lookup(query.model_copy(update={"as_of": clock.now()})) is None
    disabled = SemanticQueryPreparation(
        factory,
        clock,
        principal(),
        provider,
        CONFIG,
        admitted=lambda: True,
        context=lambda: CONTEXT,
        local=LOCAL,
    )
    assert (await disabled.prepare([query])).prepared == 0
    assert len(provider.calls) == 2
    with pytest.raises(ValueError, match="local encoder"):
        SemanticIndexingPass(
            factory,
            clock,
            principal(),
            provider,
            INDEX_CONFIG.model_copy(update={"historical_indexing": True}),
            admitted=lambda: True,
        )


async def test_historical_knowledge_is_labeled_in_rendered_evidence() -> None:
    from tests.contract.memory_fixtures import formation_stack, memory, recall_query
    from tests.contract.support import NOW, SESSION_ID

    clock, factory, _, retriever = await formation_stack()
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory(statement="User avoids dairy"))
    clock.advance(timedelta(days=1))
    async with factory() as uow:
        await uow.memories.reinforce(
            original.model_copy(update={"statement": "User now eats dairy", "store_position": 2})
        )
    query = recall_query(text="avoids dairy").model_copy(
        update={"as_of": NOW, "known_at": NOW, "candidate_selection": "relevance"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert result.items and result.items[0].statement == original.statement
    assert f'known_at="{NOW.isoformat().replace("+00:00", "Z")}"' in result.rendered
