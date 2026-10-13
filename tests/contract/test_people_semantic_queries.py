"""Identity discovery may narrow a prepared query without widening its authority."""

from datetime import timedelta
from typing import Any
from uuid import UUID

from agent_core.domain.memory import Sensitivity
from agent_core.domain.semantic_memory import query_digest
from agent_core.memory.semantic_queries import SemanticQueryContext, SemanticQueryPreparation
from tests.contract.memory_fixtures import recall_query
from tests.contract.semantic_indexing_cases import INDEX_CONFIG, Provider
from tests.contract.support import NOW, memory_uow_factory, principal


async def test_people_narrowing_preserves_encoder_input_and_hard_restrictions() -> None:
    clock, factory = await memory_uow_factory()
    provider = Provider(factory)
    active = [True]
    lookup = SemanticQueryPreparation(
        factory,
        clock,
        principal(),
        provider,
        INDEX_CONFIG,
        admitted=lambda: active[0],
        context=lambda: SemanticQueryContext(
            policy_version="policy@1",
            retrieval_policy="retrieval@1",
            surface_id="owner-chat",
            task_sha256="a" * 64,
            sensitivity_floor=Sensitivity.INTERNAL,
        ),
    )
    base = recall_query(text="Suggest a place for Alex", subjects=["Alex"]).model_copy(
        update={"candidate_selection": "relevance", "include_ids": (UUID(int=500), UUID(int=501))}
    )
    assert (await lookup.prepare([base])).prepared == 1
    narrowed = base.model_copy(
        update={
            "people_scope": (UUID(int=9000),),
            "include_ids": (UUID(int=500),),
            "expand_ids": (UUID(int=500),),
            "subjects": [],
            "sensitivity_ceiling": Sensitivity.SENSITIVE,
            "max_items": 1,
            "budget_tokens": 100,
        }
    )
    vector = lookup.lookup(narrowed)
    assert vector is not None, "Validated identity narrowing must not lose the prepared vector"
    assert vector.query_sha256 == query_digest(narrowed)
    assert lookup.lookup(base) is not None and len(provider.calls) == len(lookup._cache) == 1
    for update in (
        {"text": "A different task"},
        {"subjects": ["Unstated context"]},
        # Subjects also grant access to local-portability originals outside the
        # current project. An already present word is not permission to add one.
        {"subjects": ["place"]},
        {"tenant_id": "foreign"},
        {"principal_id": "foreign"},
        {"current_scope": "foreign"},
        {"include_ids": None},
        {"include_ids": (UUID(int=999),)},
        {"exclude_ids": (UUID(int=900),)},
        {"include_superseded": True},
        {"known_at": NOW},
        {"as_of": NOW},
        {"budget_tokens": base.budget_tokens + 1},
        {"max_items": base.max_items + 1},
        {"min_score": 0.8},
    ):
        assert lookup.lookup(narrowed.model_copy(update=update)) is None, update
    assert len(provider.calls) == 1
    active[0] = False
    assert lookup.lookup(narrowed) is None and not lookup._cache
    active[0] = True
    assert lookup.lookup(narrowed) is None, "Restoring admission cannot revive a revoked parent"
    assert (await lookup.prepare([base])).prepared == 1
    clock.advance(timedelta(seconds=301))
    assert lookup.lookup(narrowed) is None


async def test_actual_people_discovery_reuses_one_prepared_vector_before_dense_recall() -> None:
    from agent_core.application.people_context import (
        PeopleAwareMemoryRetriever,
        PeopleContextService,
    )
    from agent_core.domain.people import Person, PersonMemoryLink
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from tests.contract.memory_fixtures import memory
    from tests.contract.semantic_memory_cases import SPACE, VECTOR
    from tests.contract.support import SESSION_ID, ids

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    provider = Provider(factory)
    person_id = UUID(int=9000)
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    async with factory() as uow:
        record = await uow.memories.upsert_belief(memory(statement="Alex avoids all dairy"))
        await uow.people.put(
            Person(id=person_id, display_name="Alex", **common), expected_revision=0
        )
        await uow.people.put(
            PersonMemoryLink(id=UUID(int=9100), person_id=person_id, belief_id=record.id, **common),
            expected_revision=0,
        )
        source = await uow.semantic_memory.capture(owner, record.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(owner, source, VECTOR)
    lookup = SemanticQueryPreparation(
        factory,
        clock,
        owner,
        provider,
        INDEX_CONFIG,
        admitted=lambda: True,
        context=lambda: SemanticQueryContext(
            policy_version="policy@1",
            retrieval_policy="retrieval@1",
            surface_id="private",
            task_sha256="a" * 64,
            sensitivity_floor=Sensitivity.INTERNAL,
        ),
    )
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        ids(),
        owner,
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(
            update={"semantic_enabled": True, "semantic_channel": "dense"}
        ),
        semantic_queries=lookup,
    )
    query = recall_query(text="Plan supper with Alex", subjects=[]).model_copy(
        update={"candidate_selection": "relevance"}
    )
    assert (await lookup.prepare([query])).prepared == 1
    aware = PeopleAwareMemoryRetriever(PeopleContextService(factory, retriever), owner)
    result = await aware.recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [record.id]
    assert result.items[0].arms == ["semantic"]
    assert len(provider.calls) == 1
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, owner)
    assert trace.query.people_scope == (person_id,)
    assert (
        "+semantic@" in trace.retrieval_policy_version
        and "+dense@" in trace.retrieval_policy_version
    )
