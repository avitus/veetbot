"""Bitemporal grouping retains original atoms and applies current privacy."""

from datetime import timedelta

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.domain.memory import RecallProfile, Sensitivity
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.reconsolidation_cases import Stores
from tests.contract.reconsolidation_source_cases import seed
from tests.contract.support import NOW, SESSION_ID, principal


async def historical_undo_boundaries(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        newer = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(newer.model_copy(update={"utility": 1}))
        clock.advance(timedelta(seconds=10))
        plan = await uow.reconsolidation.plan_merge(
            principal(), job.lease_token, group.id, clock.now()
        )
        assert plan is not None
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, clock.now()
        )
    clock.advance(timedelta(seconds=10))
    middle = clock.now()
    clock.advance(timedelta(seconds=10))
    async with factory() as uow:
        terminal = await uow.reconsolidation.undo_merge(
            principal(), operation.id, 1, "history", clock.now()
        )
        head = await uow.memories.head_position(principal())
    retriever = HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    for effective, known, expected in (
        (middle, middle, operation.id),
        (middle, clock.now(), operation.id),
        (clock.now(), middle, operation.id),
        (operation.committed_at, operation.committed_at, operation.id),
        (NOW, clock.now(), None),
        (middle, NOW, None),
        (clock.now(), clock.now(), None),
    ):
        result = await retriever.recall(
            recall_query().model_copy(update={"as_of": effective, "known_at": known}),
            session_id=SESSION_ID,
        )
        assert len(result.items) == 1
        assert result.items[0].merge_id == expected, "membership must follow both temporal cutoffs"
        assert result.items[0].belief_id == (plan.canonical_id if expected else newer.id)
        if expected:
            assert result.items[0].merge_revision == 1
    async with factory() as uow:
        assert (
            await uow.reconsolidation.get_merge(principal(), operation.id, clock.now()) == terminal
        )
        assert await uow.memories.head_position(principal()) == head


async def historical_correction_and_privacy(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
    clock.advance(timedelta(seconds=20))
    async with factory() as uow:
        original = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(original.model_copy(update={"statement": "Detailed answers"}))
    retriever = HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    query = recall_query(text=None).model_copy(
        update={
            "profile": RecallProfile.CORE,
            "as_of": NOW,
            "known_at": NOW,
            "sensitivity_ceiling": Sensitivity.INTERNAL,
        }
    )
    historical = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.merge_id for item in historical.items] == [operation.id]
    current_knowledge = await retriever.recall(
        query.model_copy(update={"known_at": clock.now()}), session_id=SESSION_ID
    )
    assert {item.belief_id for item in current_knowledge.items} == set(plan.member_ids)
    assert all(item.merge_id is None for item in current_knowledge.items)
    async with factory() as uow:
        await uow.memories.reinforce(
            original.model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
        )
    hidden = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in hidden.items] == [plan.canonical_id]
    assert hidden.items[0].merge_id is None, "hidden support cannot certify historical membership"
    async with factory() as uow:
        await uow.memories.fence_for_erasure(principal(), [plan.canonical_id])
    assert (await retriever.recall(query, session_id=SESSION_ID)).items == []


HISTORY_SCENARIOS = [historical_undo_boundaries, historical_correction_and_privacy]


async def historical_scope_and_lookup_bound(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    import pytest

    from agent_core.domain.memory import Portability

    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
    retriever = HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    query = recall_query().model_copy(update={"as_of": NOW, "known_at": NOW})
    fallback = await retriever.recall(
        query.model_copy(update={"exclude_ids": (plan.canonical_id,)}), session_id=SESSION_ID
    )
    assert len(fallback.items) == 1 and fallback.items[0].merge_id == operation.id
    disabled = HybridMemoryRetriever(factory, clock, RandomIdFactory(), principal())
    assert (await disabled.recall(query, session_id=SESSION_ID)).items[0].merge_id is None
    clock.advance(timedelta(seconds=10))
    async with factory() as uow:
        with pytest.raises(ValueError, match="1000"):
            await uow.reconsolidation.merges_at(
                principal(), (plan.canonical_id,) * 1001, as_of=NOW, known_at=NOW
            )
        assert (
            await uow.reconsolidation.merges_at(
                principal().model_copy(update={"principal_id": "foreign"}),
                plan.member_ids,
                as_of=NOW,
                known_at=NOW,
            )
            == ()
        )
        original = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(
            original.model_copy(
                update={
                    "scope": "another-project",
                    "portability": Portability.LOCAL,
                }
            )
        )
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [plan.canonical_id]
    assert result.items[0].merge_id is None, "current source scope applies to historical grouping"


async def historical_expiry_and_same_instant_versions(
    factory: UnitOfWorkFactory, clock: FixedClock
) -> None:
    from tests.contract.reconsolidation_source_cases import queue_sources, seed_sources

    async with factory() as uow:
        stores = Stores(uow.memories, uow.reconsolidation, uow.events, uow.people)
        await seed_sources(stores)
        for key in (memory().id, memory(belief_id=502).id):
            record = await uow.memories.get(key, principal())
            await uow.memories.reinforce(
                record.model_copy(update={"expires_at": NOW + timedelta(seconds=30)})
            )
        job, group = await queue_sources(stores)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
    clock.advance(timedelta(seconds=40))
    retriever = HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    query = recall_query().model_copy(update={"as_of": NOW, "known_at": NOW})
    assert (await retriever.recall(query, session_id=SESSION_ID)).items[0].merge_id == operation.id
    assert (
        await retriever.recall(
            query.model_copy(update={"as_of": clock.now()}), session_id=SESSION_ID
        )
    ).items == []
    async with factory() as uow:
        # Returning to identical prose does not roll back the content revision.
        original = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(original.model_copy(update={"statement": "Other preference"}))
        await uow.memories.reinforce(original)
        head = await uow.memories.head_position(principal())
    result = await retriever.recall(
        query.model_copy(update={"known_at": clock.now()}), session_id=SESSION_ID
    )
    assert len(result.items) == 1 and result.items[0].merge_id is None
    assert (await retriever.recall(query, session_id=SESSION_ID)).items[0].merge_id == operation.id
    async with factory() as uow:
        assert await uow.memories.head_position(principal()) == head


HISTORY_SCENARIOS.extend(
    [historical_scope_and_lookup_bound, historical_expiry_and_same_instant_versions]
)


async def historical_attribution_and_erasure(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    from uuid import UUID

    from agent_core.domain.people import PeopleSource
    from agent_core.domain.people_sources import source_id
    from tests.contract.reconsolidation_attribution_cases import link, person
    from tests.contract.reconsolidation_source_cases import queue_sources, seed_sources

    source = PeopleSource(
        id=source_id(principal(), SESSION_ID, 1),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        created_at=NOW,
        updated_at=NOW,
        sensitivity=Sensitivity.INTERNAL,
        session_id=SESSION_ID,
        event_sequence=1,
        source_kind="owner",
        evidence_at=NOW,
        source_revision="source@1",
    )
    links = tuple(
        link(501 + i).model_copy(update={"id": UUID(int=811 + i), "support_ids": [source.id]})
        for i in range(2)
    )
    async with factory() as uow, uow.people.lock(principal()):
        stores = Stores(uow.memories, uow.reconsolidation, uow.events, uow.people)
        await seed_sources(stores)
        await uow.people.put(source, expected_revision=0)
        await uow.people.put(person(), expected_revision=0)
        await uow.people.put(person(802), expected_revision=0)
        for value in links:
            await uow.people.put(value, expected_revision=0)
        job, group = await queue_sources(stores)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
    clock.advance(timedelta(seconds=20))
    async with factory() as uow, uow.people.lock(principal()):
        for value in links:
            await uow.people.put(
                value.model_copy(
                    update={
                        "person_id": person(802).id,
                        "revision": 2,
                        "updated_at": clock.now(),
                    }
                ),
                expected_revision=1,
            )
    retriever = HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    query = recall_query().model_copy(
        update={"as_of": NOW, "known_at": NOW, "people_scope": (person().id,)}
    )
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.merge_id for item in result.items] == [operation.id]
    assert (
        await retriever.recall(
            query.model_copy(update={"known_at": clock.now()}), session_id=SESSION_ID
        )
    ).items == []
    async with factory() as uow, uow.people.lock(principal()):
        await uow.people.fence_for_erasure(principal(), [person().id])
        assert (
            await uow.reconsolidation.merges_at(
                principal(), plan.member_ids, as_of=NOW, known_at=NOW
            )
            == ()
        )
        await uow.people.purge_erased(principal())
        assert (
            await uow.reconsolidation.merges_at(
                principal(), plan.member_ids, as_of=NOW, known_at=NOW
            )
            == ()
        )


HISTORY_SCENARIOS.append(historical_attribution_and_erasure)
