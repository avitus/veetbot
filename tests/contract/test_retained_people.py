"""Saved People evidence preserves support, identity and temporal boundaries."""

import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.people_context import PeopleContextService
from agent_core.domain.memory import BeliefType, Sensitivity
from agent_core.domain.messages import MemoryRecallUsage
from agent_core.domain.people import PeopleEndpoint, Person, RelationshipAssertion
from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, RUN_ID, SESSION_ID, ids, memory_uow_factory, principal

CHANGES = [None, "person", "private", "erased", "ambiguous", "edge_expiry", "belief_expiry"]


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("max_items", [1, 20])
async def test_retained_people_dependencies(change: str | None, max_items: int) -> None:
    clock, factory = await memory_uow_factory()
    await retained_people_contract(clock, factory, change, max_items)


async def retained_people_contract(
    clock: FixedClock, factory: UnitOfWorkFactory, change: str | None, max_items: int
) -> None:
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Quinn", **common)
    belief = memory(belief_id=812, statement="Quinn is the user's accountant.").model_copy(
        update={
            "subject": "Quinn",
            "belief_type": BeliefType.RELATIONSHIP,
            "expires_at": NOW + timedelta(minutes=1) if change == "belief_expiry" else None,
        }
    )
    edge = RelationshipAssertion(
        id=uuid4(),
        subject=PeopleEndpoint(kind="person", id=person.id),
        object=PeopleEndpoint(kind="owner"),
        predicate="other",
        qualifier="accountant",
        belief_id=belief.id,
        valid_to=NOW + timedelta(minutes=1) if change == "edge_expiry" else None,
        **common,
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(edge, expected_revision=0)
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        ids(),
        owner,
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(
            update={"candidate_selection": "relevance", "reranking": "relevance"}
        ),
        reranking_admitted=lambda: True,
    )
    service = PeopleContextService(factory, retriever)
    result = await service.automatic_recall(
        owner,
        recall_query(text="Prepare for my accountant.", max_items=max_items).model_copy(
            update={"candidate_selection": "relevance"}
        ),
        session_id=SESSION_ID,
        run_id=RUN_ID,
    )
    assert result.people and result.people[0].record_id == person.id
    if max_items == 1:
        assert result.items == [] and len(result.people) == 1, "Discovery proof is not rendered"
    usage = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items) + len(result.people),
        content_sha256=hashlib.sha256(result.rendered.encode()).hexdigest(),
    )
    assert await retriever.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID)
    async with factory() as uow:
        if change in {"person", "private"}:
            await uow.people.put(
                person.model_copy(
                    update={
                        "revision": 2,
                        "display_name": "Changed" if change == "person" else "Quinn",
                        "sensitivity": Sensitivity.RESTRICTED
                        if change == "private"
                        else person.sensitivity,
                    }
                ),
                expected_revision=1,
            )
        elif change == "erased":
            await uow.memories.fence_for_erasure(owner, [belief.id])
        elif change == "ambiguous":
            other = person.model_copy(update={"id": uuid4(), "display_name": "Morgan"})
            await uow.people.put(other, expected_revision=0)
            await uow.people.put(
                edge.model_copy(
                    update={"id": uuid4(), "subject": PeopleEndpoint(kind="person", id=other.id)}
                ),
                expected_revision=0,
            )
    if change in {"edge_expiry", "belief_expiry"}:
        clock.advance(timedelta(minutes=2))
    assert await retriever.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID) is (
        change is None
    )
