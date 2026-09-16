"""Behavioral contract for the people recall composition interface."""

from uuid import uuid4

from agent_core.application.people_context import PeopleContextService
from agent_core.domain.people import Person, PersonMemoryLink
from agent_core.memory.retrieval import HybridMemoryRetriever
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, SESSION_ID, ids, memory_uow_factory, principal


async def test_people_context_selects_only_linked_facts_and_records_identity() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    people = [Person(id=uuid4(), display_name="Alex", **common) for _ in range(2)]
    beliefs = [memory(belief_id=500 + i, statement=f"Alex likes activity {i}") for i in range(2)]
    async with factory() as uow:
        for person, belief in zip(people, beliefs, strict=True):
            await uow.people.put(person, expected_revision=0)
            await uow.memories.upsert_belief(belief)
            await uow.people.put(
                PersonMemoryLink(id=uuid4(), person_id=person.id, belief_id=belief.id, **common),
                expected_revision=0,
            )
    from agent_core.ports.people_runtime import PeopleRecall

    retriever: PeopleRecall = HybridMemoryRetriever(factory, clock, ids(), owner)
    service = PeopleContextService(factory, retriever)
    result = await service.recall(
        owner, [people[0].id], recall_query(text="Alex"), session_id=SESSION_ID
    )
    assert [item.belief_id for item in result.items] == [beliefs[0].id]
    assert result.people[0].record_id == people[0].id
    assert result.tokens <= 500
