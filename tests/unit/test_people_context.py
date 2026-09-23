"""People context never blends same-name identities or widens local memory scope."""

from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.application.people_context import PeopleContextService
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleQuery, PeopleRecord, Person, PersonMemoryLink
from agent_core.memory.retrieval import HybridMemoryRetriever
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, SESSION_ID, ids, memory_uow_factory, principal


async def test_ordinary_recall_registers_influence_before_forget_can_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from agent_core.adapters.memory.in_memory import InMemoryMemoryStore
    from agent_core.domain.errors import NotFoundError
    from agent_core.domain.memory import MemoryRecord, RecallQuery

    clock, factory = await memory_uow_factory()
    owner = principal()
    belief = memory()
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
    queried, release, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_query = InMemoryMemoryStore.query

    async def paused_query(self: InMemoryMemoryStore, query: RecallQuery) -> list[MemoryRecord]:
        result = await original_query(self, query)
        queried.set()
        await release.wait()
        return result

    monkeypatch.setattr(InMemoryMemoryStore, "query", paused_query)

    async def erase() -> None:
        async with factory() as uow:
            attempting.set()
            async with uow.people.lock(owner):
                await uow.memories.fence_for_erasure(owner, [belief.id])
                await uow.traces.erase_people(owner, [], [belief.id])

    retriever = HybridMemoryRetriever(factory, clock, ids(), owner)
    recalling = asyncio.create_task(retriever.recall(recall_query(), session_id=SESSION_ID))
    await asyncio.wait_for(queried.wait(), timeout=2)
    erasing = asyncio.create_task(erase())
    try:
        await asyncio.wait_for(attempting.wait(), timeout=2)
        done, _ = await asyncio.wait([erasing], timeout=0.05)
        assert not done, "forget must wait until recall has registered its derived influence"
    finally:
        release.set()
        result, _ = await asyncio.gather(recalling, erasing)
    assert result.items
    async with factory() as uow:
        with pytest.raises(NotFoundError):
            await uow.traces.get(result.trace_id, owner)


async def test_linked_facts_cannot_starve_person_and_interaction_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.persistence.people import InMemoryPeopleStore
    from agent_core.domain.people import InteractionParticipant, PeopleInteraction

    calls = 0
    original_query = InMemoryPeopleStore.query

    async def counted_query(self: InMemoryPeopleStore, query: PeopleQuery) -> list[PeopleRecord]:
        nonlocal calls
        calls += 1
        return await original_query(self, query)

    monkeypatch.setattr(InMemoryPeopleStore, "query", counted_query)

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Maya", **common)
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        for index in range(25):
            belief = memory(
                belief_id=700 + index, statement=f"Maya discussed topic {index}."
            ).model_copy(update={"subject": f"Maya topic {index}"})
            await uow.memories.upsert_belief(belief)
            await uow.people.put(
                PersonMemoryLink(
                    id=uuid4(),
                    person_id=person.id,
                    belief_id=belief.id,
                    **common,
                ),
                expected_revision=0,
            )
        await uow.people.put(
            PeopleInteraction(
                id=uuid4(),
                channel="chat",
                interaction_kind="meeting",
                attribution="owner_reported",
                direction="reported",
                summary="Lunch with Maya",
                occurred_at=NOW,
                participants=[InteractionParticipant(person_id=person.id, role="participant")],
                **common,
            ),
            expected_revision=0,
        )
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    result = await service.recall(
        owner,
        [person.id],
        recall_query(
            text="Maya",
            budget_tokens=2000,
            max_items=20,
        ),
        session_id=SESSION_ID,
    )
    assert {item.kind for item in result.people} >= {"person", "interaction"}
    assert result.items and len(result.items) + len(result.people) <= 20
    assert result.tokens <= 2000
    assert calls <= 8, "People recall must batch identity checks instead of querying per belief"


async def test_trace_people_view_rechecks_live_privacy() -> None:
    from datetime import timedelta

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    person = Person(
        id=uuid4(),
        display_name="Sam",
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
    )
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
    run_id = uuid4()
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    result = await service.recall(
        owner, [person.id], recall_query(text="Sam"), session_id=SESSION_ID, run_id=run_id
    )
    async with factory() as uow:
        view = await uow.traces.user_view(run_id, "private", "sensitive")
        assert view.people == result.people
        await uow.people.put(
            person.model_copy(
                update={
                    "sensitivity": Sensitivity.RESTRICTED,
                    "revision": 2,
                    "updated_at": NOW + timedelta(seconds=1),
                }
            ),
            expected_revision=1,
        )
        assert (await uow.traces.user_view(run_id, "private", "sensitive")).people == []


async def test_automatic_recall_adds_unambiguous_history_but_keeps_ambiguous_names_apart() -> None:
    from agent_core.domain.people import InteractionParticipant, PeopleInteraction

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    maya = Person(id=uuid4(), display_name="Maya", **common)
    alexes = [Person(id=uuid4(), display_name="Alex", **common) for _ in range(2)]
    async with factory() as uow:
        for person in [maya, *alexes]:
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PeopleInteraction(
                    id=uuid4(),
                    channel="chat",
                    interaction_kind="meeting",
                    attribution="owner_reported",
                    direction="reported",
                    summary=f"Lunch with {person.display_name}",
                    participants=[InteractionParticipant(person_id=person.id, role="participant")],
                    **common,
                ),
                expected_revision=0,
            )
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    automatic = getattr(service, "automatic_recall", None)
    assert automatic is not None, "People history must participate in automatic task recall"
    result = await automatic(
        owner,
        recall_query(text="Prepare for my next meeting with Maya and Alex"),
        session_id=SESSION_ID,
    )
    assert any(
        item.kind == "interaction" and item.person_ids == [maya.id] for item in result.people
    )
    assert all(not set(item.person_ids) & {p.id for p in alexes} for item in result.people)
    assert result.tokens <= 500


async def test_explicit_session_person_selection_disambiguates_same_name() -> None:
    from agent_core.domain.people import InteractionParticipant, PeopleInteraction
    from tests.contract.support import session

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    people = [Person(id=uuid4(), display_name="Alex", **common) for _ in range(2)]
    selected_session = session().model_copy(
        update={"id": uuid4(), "metadata": {"people_person_ids": [str(people[0].id)]}}
    )
    async with factory() as uow:
        await uow.sessions.create(selected_session)
        for person in people:
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PeopleInteraction(
                    id=uuid4(),
                    channel="chat",
                    interaction_kind="meeting",
                    attribution="owner_reported",
                    direction="reported",
                    summary="Lunch meeting",
                    participants=[InteractionParticipant(person_id=person.id, role="participant")],
                    **common,
                ),
                expected_revision=0,
            )
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    result = await service.automatic_recall(
        owner, recall_query(text="Prepare for Alex"), session_id=selected_session.id
    )
    assert result.people and all(item.person_ids == [people[0].id] for item in result.people)


async def test_automatic_people_selection_excludes_other_same_name_beliefs() -> None:
    from tests.contract.support import session

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    people = [
        Person(
            id=uuid4(),
            display_name="Alex",
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            created_at=NOW,
            updated_at=NOW,
        )
        for _ in range(2)
    ]
    selected_session = session().model_copy(
        update={"id": uuid4(), "metadata": {"people_person_ids": [str(people[0].id)]}}
    )
    beliefs = [memory(belief_id=901 + i, statement=f"Alex prefers activity {i}") for i in range(2)]
    async with factory() as uow:
        await uow.sessions.create(selected_session)
        for person, belief in zip(people, beliefs, strict=True):
            await uow.people.put(person, expected_revision=0)
            await uow.memories.upsert_belief(belief)
            await uow.people.put(
                PersonMemoryLink(
                    id=uuid4(),
                    person_id=person.id,
                    belief_id=belief.id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision=0,
            )
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    selected = await service.automatic_recall(
        owner, recall_query(text="Alex prefers activity"), session_id=selected_session.id
    )
    assert [item.belief_id for item in selected.items] == [beliefs[0].id]
    ambiguous = await service.automatic_recall(
        owner, recall_query(text="Alex prefers activity"), session_id=SESSION_ID
    )
    assert ambiguous.items == []


async def test_context_includes_direct_relationship_and_open_commitment_without_transitivity() -> (
    None
):
    from agent_core.domain.people import (
        PeopleCommitment,
        PeopleEndpoint,
        PeopleSource,
        RelationshipAssertion,
    )

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    maya, jules, alex = [
        Person(id=uuid4(), display_name=name, **common) for name in ("Maya", "Jules", "Alex")
    ]
    source = PeopleSource(
        id=uuid4(),
        session_id=SESSION_ID,
        event_sequence=1,
        source_kind="owner",
        evidence_at=NOW,
        source_revision="fixture",
        **common,
    )
    belief = memory(statement="Maya introduced Jules and I promised Maya a book.")
    async with factory() as uow:
        for row in (maya, jules, alex, source):
            await uow.people.put(row, expected_revision=0)
        await uow.memories.upsert_belief(belief)
        for subject, target in ((jules, maya), (alex, jules)):
            await uow.people.put(
                RelationshipAssertion(
                    id=uuid4(),
                    subject=PeopleEndpoint(kind="person", id=subject.id),
                    object=PeopleEndpoint(kind="person", id=target.id),
                    predicate="introduced_by",
                    belief_id=belief.id,
                    support_ids=[source.id],
                    **common,
                ),
                expected_revision=0,
            )
        await uow.people.put(
            PeopleCommitment(
                id=uuid4(),
                debtor=PeopleEndpoint(kind="owner"),
                beneficiary=PeopleEndpoint(kind="person", id=maya.id),
                description="I promised Maya a book",
                state="open",
                state_source_id=source.id,
                belief_id=belief.id,
                support_ids=[source.id],
                **common,
            ),
            expected_revision=0,
        )
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    result = await service.recall(
        owner, [maya.id], recall_query(text="Maya", budget_tokens=2000), session_id=SESSION_ID
    )
    relations = [item for item in result.people if item.kind == "relationship"]
    assert len(relations) == 1 and "Jules" in relations[0].text and "Maya" in relations[0].text
    assert all(alex.id not in item.person_ids for item in result.people)
    assert any(
        item.kind == "commitment" and "owner" in item.text and "open" in item.text
        for item in result.people
    )


@pytest.mark.parametrize("automatic", [False, True])
async def test_historical_context_keeps_exact_cutoff_and_original_identity(automatic: bool) -> None:
    from datetime import timedelta

    from agent_core.domain.people import InteractionParticipant, PeopleInteraction

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Maya", **common)
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        for offset in (0, 1):
            await uow.people.put(
                PeopleInteraction(
                    id=uuid4(),
                    channel="chat",
                    interaction_kind="meeting",
                    attribution="owner_reported",
                    direction="reported",
                    summary=f"Meeting {offset}",
                    occurred_at=NOW + timedelta(seconds=offset),
                    participants=[InteractionParticipant(person_id=person.id, role="participant")],
                    **common,
                ),
                expected_revision=0,
            )
        await uow.people.put(
            person.model_copy(
                update={
                    "display_name": "Future name",
                    "revision": 2,
                    "updated_at": NOW + timedelta(days=2),
                }
            ),
            expected_revision=1,
        )
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    query = recall_query(text="Maya").model_copy(
        update={"as_of": NOW, "known_at": NOW + timedelta(days=1)}
    )
    result = (
        await service.automatic_recall(owner, query, session_id=SESSION_ID)
        if automatic
        else await service.recall(owner, [person.id], query, session_id=SESSION_ID)
    )
    assert "Future name" not in result.rendered
    assert "Meeting 0" in result.rendered
    assert "Meeting 1" not in result.rendered


async def test_knowledge_recall_registers_before_people_erasure_can_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import asyncio

    from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
    from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
    from agent_core.domain.knowledge import KnowledgeQuery, RetrievedPassage
    from agent_core.knowledge.service import KnowledgeService
    from tests.contract.memory_fixtures import prepared_knowledge

    clock, factory = await memory_uow_factory()
    owner = principal()
    prepared = prepared_knowledge()
    async with factory() as uow:
        await uow.knowledge.ingest(prepared)
    queried, release, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_search = InMemoryKnowledgeStore.search

    async def paused_search(
        self: InMemoryKnowledgeStore, query: KnowledgeQuery
    ) -> list[RetrievedPassage]:
        result = await original_search(self, query)
        queried.set()
        await release.wait()
        return result

    monkeypatch.setattr(InMemoryKnowledgeStore, "search", paused_search)

    async def erase() -> None:
        async with factory() as uow:
            attempting.set()
            async with uow.people.lock(owner):
                await uow.knowledge.delete(prepared.document.document_id, owner)
                await uow.traces.mark_document_deleted(
                    owner.tenant_id, prepared.document.document_id
                )

    service = KnowledgeService(factory, FilesystemArtifactStore(tmp_path), clock, ids(), owner)
    recalling = asyncio.create_task(
        service.search(
            KnowledgeQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                current_scope=None,
                text="restart service",
                budget_tokens=1000,
                max_passages=5,
                min_score=0.1,
            ),
            session_id=SESSION_ID,
        )
    )
    await asyncio.wait_for(queried.wait(), timeout=2)
    erasing = asyncio.create_task(erase())
    try:
        await asyncio.wait_for(attempting.wait(), timeout=2)
        done, _ = await asyncio.wait([erasing], timeout=0.05)
        assert not done, "forget must wait until knowledge recall has recorded its influence"
    finally:
        release.set()
        result, _ = await asyncio.gather(recalling, erasing)
    assert result.passages
    async with factory() as uow:
        stored = await uow.traces.get(result.trace_id, owner)
        assert all(passage.deleted and passage.text is None for passage in stored.passages)


async def _person_with_interaction(
    factory: object, owner: object, display_name: str, *, name_copies: int = 0
) -> Person:
    from agent_core.domain.people import (
        InteractionParticipant,
        PeopleInteraction,
        PersonIdentifier,
    )

    common: PeopleFields = {
        "tenant_id": owner.tenant_id,  # type: ignore[attr-defined]
        "principal_id": owner.principal_id,  # type: ignore[attr-defined]
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name=display_name, **common)
    async with factory() as uow:  # type: ignore[operator]
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(
            PeopleInteraction(
                id=uuid4(),
                channel="chat",
                interaction_kind="meeting",
                attribution="owner_reported",
                direction="reported",
                summary=f"Lunch with {display_name}",
                participants=[InteractionParticipant(person_id=person.id, role="participant")],
                **common,
            ),
            expected_revision=0,
        )
        for _ in range(name_copies):
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="name",
                    namespace="owner",
                    value=display_name,
                    context="email:sender",
                    verification="contextual",
                    valid_from=NOW,
                    **common,
                ),
                expected_revision=0,
            )
    return person


async def test_frequent_correspondent_still_selects_person_context() -> None:
    """Per-message name copies of one person never make context selection abstain."""
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    alex = await _person_with_interaction(factory, owner, "Alex Rivera", name_copies=150)
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    result = await service.automatic_recall(
        owner, recall_query(text="Prepare for my call with Alex Rivera"), session_id=SESSION_ID
    )
    assert any(alex.id in item.person_ids for item in result.people)


async def test_pronoun_named_person_is_never_selected_for_context() -> None:
    """A person labelled 'I' must not ride along with every first-person request."""
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    pronoun = await _person_with_interaction(factory, owner, "I")
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    result = await service.automatic_recall(
        owner, recall_query(text="I need to prepare for the board meeting"), session_id=SESSION_ID
    )
    assert all(pronoun.id not in item.person_ids for item in result.people)
