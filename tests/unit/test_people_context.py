"""People context never blends same-name identities or widens local memory scope."""

from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.people_context import PeopleContextService
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleQuery, PeopleRecord, Person, PersonMemoryLink
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, SESSION_ID, ids, memory_uow_factory, principal


@pytest.mark.parametrize("role", ["father", "accountant", "coach", "emergency contact"])
@pytest.mark.parametrize(
    "restriction",
    [None, "ambiguous", "private", "expired", "unresolved", "indirect", "excluded", "permission"],
)
async def test_relationship_reference_retrieves_linked_facts_without_a_name(
    role: str, restriction: str | None
) -> None:
    clock, factory = await memory_uow_factory()
    await relationship_recall_contract(clock, factory, role, restriction)


async def test_relationship_links_survive_more_recent_topical_distractors() -> None:
    clock, factory = await memory_uow_factory()
    await relationship_recall_contract(clock, factory, "coach", None, distractors=256)


async def relationship_recall_contract(
    clock: FixedClock,
    factory: UnitOfWorkFactory,
    role: str,
    restriction: str | None,
    *,
    distractors: int = 0,
) -> None:
    from agent_core.domain.memory import BeliefType
    from agent_core.domain.people import PeopleEndpoint, RelationshipAssertion
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE

    owner = principal().model_copy(
        update={"scopes": set() if restriction == "permission" else {"people.read"}}
    )
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Quinn", **common)
    other = Person(id=uuid4(), display_name="Morgan", **common)
    relationship = memory(belief_id=901, statement=f"Quinn is the user's {role}.").model_copy(
        update={"subject": "Quinn", "belief_type": BeliefType.RELATIONSHIP}
    )
    interest = memory(belief_id=902, statement="Quinn enjoys botanical illustration.").model_copy(
        update={"subject": "Quinn", "belief_type": BeliefType.USER_MODEL_ATTR, "store_position": 2}
    )
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(other, expected_revision=0)
        for index in range(distractors):
            await uow.memories.upsert_belief(
                memory(
                    belief_id=1000 + index, statement=f"Exhibition logistics for venue {index}."
                ).model_copy(update={"store_position": index + 3, "subject": f"Venue {index}"})
            )
        for belief in (relationship, interest):
            await uow.memories.upsert_belief(belief)
            await uow.people.put(
                PersonMemoryLink(id=uuid4(), person_id=person.id, belief_id=belief.id, **common),
                expected_revision=0,
            )
        edge = RelationshipAssertion(
            id=uuid4(),
            subject=PeopleEndpoint(kind="person", id=person.id),
            object=PeopleEndpoint(kind="person", id=other.id)
            if restriction == "indirect"
            else PeopleEndpoint(kind="owner"),
            predicate="other",
            qualifier=role,
            belief_id=relationship.id,
            unresolved=restriction == "unresolved",
            valid_to=NOW if restriction == "expired" else None,
            **common,
        )
        if restriction == "private":
            edge = edge.model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
        await uow.people.put(edge, expected_revision=0)
        if restriction == "ambiguous":
            await uow.people.put(
                edge.model_copy(
                    update={"id": uuid4(), "subject": PeopleEndpoint(kind="person", id=other.id)}
                ),
                expected_revision=0,
            )
    profile = DEFAULT_RETRIEVAL_PROFILE.model_copy(
        update={"candidate_selection": "relevance", "reranking": "relevance"}
    )
    service = PeopleContextService(
        factory,
        HybridMemoryRetriever(
            factory, clock, ids(), owner, profile=profile, reranking_admitted=lambda: True
        ),
    )
    query = recall_query(
        text=f"Choose an exhibition for my {role}.", budget_tokens=2000
    ).model_copy(
        update={
            "candidate_selection": "relevance",
            "exclude_ids": (relationship.id,) if restriction == "excluded" else (),
        }
    )
    result = await service.automatic_recall(owner, query, session_id=SESSION_ID)
    if restriction:
        assert interest.id not in {item.belief_id for item in result.items}
        assert result.people == []
        return
    assert interest.id in {item.belief_id for item in result.items}
    assert any(item.kind == "person" and item.person_ids == [person.id] for item in result.people)
    assert "botanical illustration" in result.rendered
    assert result.tokens <= 2000 and len(result.items) + len(result.people) <= 20


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


@pytest.mark.parametrize("count", [0, 4, "duplicate"])
async def test_people_context_takes_one_to_three_distinct_identities(count: int | str) -> None:
    from agent_core.domain.errors import ToolValidationError

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    person_ids = [uuid4()] * 2 if count == "duplicate" else [uuid4() for _ in range(int(count))]
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))

    with pytest.raises(ToolValidationError, match="one to three distinct identities"):
        await service.recall(owner, person_ids, recall_query(text="Maya"), session_id=SESSION_ID)


@pytest.mark.parametrize(
    ("scopes", "query_identity", "error"),
    [
        ({"memory.read"}, {}, "AuthorizationError"),
        ({"people.read"}, {"principal_id": "principal-b"}, "NotFoundError"),
        ({"people.read"}, {"tenant_id": "tenant-b"}, "NotFoundError"),
    ],
)
async def test_people_context_is_owner_bound_and_scope_gated(
    scopes: set[str], query_identity: dict[str, str], error: str
) -> None:
    """Another principal's query never reads this owner's people, even by exact id."""

    from agent_core.domain import errors

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": scopes})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Maya", **common)
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))

    with pytest.raises(getattr(errors, error)):
        await service.recall(
            owner,
            [person.id],
            recall_query(text="Maya").model_copy(update=query_identity),
            session_id=SESSION_ID,
        )


@pytest.mark.parametrize("multiple_edges", [False, True])
@pytest.mark.parametrize("token_limited", [False, True])
async def test_overview_cursor_never_skips_undelivered_relationship_context(
    multiple_edges: bool,
    token_limited: bool,
) -> None:
    from uuid import UUID

    from agent_core.domain.memory import TracedPersonContext

    clock, factory = await memory_uow_factory()
    retriever = HybridMemoryRetriever(factory, clock, ids(), principal())
    first, second, third = (UUID(int=90000 + n) for n in range(3))
    contexts = [
        TracedPersonContext(
            record_id=UUID(int=91000 + n),
            revision=1,
            person_ids=[person],
            kind="relationship",
            text="A supported family relationship.",
            sensitivity=Sensitivity.INTERNAL,
        )
        for n, person in enumerate(
            [first, second, second, third] if multiple_edges else [first, second, third]
        )
    ]
    query = recall_query().model_copy(
        update={
            "relationship_group": "family",
            "next_people_after": third,
            "max_items": 20 if token_limited else 2,
            "budget_tokens": 250 if token_limited else 2000,
        }
    )
    result = await retriever.recall(
        query,
        session_id=SESSION_ID,
        people_items=contexts,
        measure_rendered_tokens=(lambda text: 100 + 100 * text.count("<person-context "))
        if token_limited
        else None,
    )
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    expected = first if multiple_edges or token_limited else second
    assert trace.query.next_people_after == expected
    assert f'people_after="{expected}"' in result.rendered
    assert result.tokens <= query.budget_tokens


@pytest.mark.parametrize("continued", [False, True])
async def test_oversized_first_person_explicitly_advances_to_later_people(continued: bool) -> None:
    from uuid import UUID

    from agent_core.domain.memory import TracedPersonContext

    clock, factory = await memory_uow_factory()
    retriever = HybridMemoryRetriever(factory, clock, ids(), principal())
    first, second = UUID(int=93000), UUID(int=93001)
    contexts = [
        TracedPersonContext(
            record_id=UUID(int=94000 + n),
            revision=1,
            person_ids=[person],
            kind="relationship",
            text="A supported family relationship.",
            sensitivity=Sensitivity.INTERNAL,
        )
        for n, person in enumerate([first] * 30 + [second])
    ]
    query = recall_query().model_copy(
        update={
            "relationship_group": "family",
            "people_after": UUID(int=92999) if continued else None,
            "max_items": 20,
            "budget_tokens": 2000,
        }
    )
    result = await retriever.recall(query, session_id=SESSION_ID, people_items=contexts)
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    assert trace.query.next_people_after == first
    assert f"Incomplete context for person {first}" in result.rendered
    assert f'people_after="{first}"' in result.rendered
    assert "not paginated by people.context" in result.rendered
    assert "narrower text query" not in result.rendered
    assert result.truncated and result.tokens <= query.budget_tokens
    # The next People-service page excludes the passed person, as its existing
    # owner-overview contract requires; retrieval must expose the later identity.
    following = await retriever.recall(
        query.model_copy(update={"people_after": first}),
        session_id=SESSION_ID,
        people_items=[contexts[-1]],
    )
    assert str(contexts[-1].record_id) in following.rendered
    assert "Incomplete context" not in following.rendered


async def test_overview_first_person_uses_full_budget_before_competing_beliefs() -> None:
    """A fitting person is not skipped because ordinary beliefs took their slots."""
    from uuid import UUID

    from agent_core.domain.memory import TracedPersonContext

    clock, factory = await memory_uow_factory()
    retriever = HybridMemoryRetriever(factory, clock, ids(), principal())
    first, second = UUID(int=95000), UUID(int=95001)
    contexts = [
        TracedPersonContext(
            record_id=UUID(int=96000 + n),
            revision=1,
            person_ids=[person],
            kind="relationship",
            text="A supported family relationship.",
            sensitivity=Sensitivity.INTERNAL,
        )
        for n, person in enumerate([first] * 9 + [second])
    ]
    async with factory() as uow:
        for n in range(20):
            await uow.memories.upsert_belief(
                memory(belief_id=97000 + n, statement=f"Family fact {n}.").model_copy(
                    update={"subject": f"Subject {n}", "store_position": n + 1}
                )
            )
    query = recall_query(text="family").model_copy(
        update={
            "relationship_group": "family",
            "max_items": 20,
            "budget_tokens": 4000,
        }
    )
    result = await retriever.recall(query, session_id=SESSION_ID, people_items=contexts)
    assert result.items, "Competing ordinary beliefs must actually be eligible"
    assert all(str(item.record_id) in result.rendered for item in contexts[:9])
    assert "Incomplete context" not in result.rendered
    assert result.tokens <= query.budget_tokens
