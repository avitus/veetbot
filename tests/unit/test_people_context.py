"""People context never blends same-name identities or widens local memory scope."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.people_context import PeopleContextService
from agent_core.domain.memory import MemoryRecord, Sensitivity
from agent_core.domain.people import PeopleQuery, PeopleRecord, Person, PersonMemoryLink
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, SESSION_ID, ids, memory_uow_factory, principal


@pytest.mark.parametrize("belief_count", [1, 5])
@pytest.mark.parametrize("extra_kind", ["interaction", "relationship", "commitment"])
async def test_focused_search_keeps_beliefs_before_filling_spare_space_with_interactions(
    belief_count: int,
    extra_kind: Literal["interaction", "relationship", "commitment"],
) -> None:
    """Verify focused search keeps beliefs before filling spare space with interactions."""
    from uuid import UUID

    from agent_core.domain.memory import TracedPersonContext
    from agent_core.tools.memory_search import MemorySearchTool

    clock, factory = await memory_uow_factory()
    retriever = HybridMemoryRetriever(factory, clock, ids(), principal())
    tool = MemorySearchTool(
        retriever,
        model_planning=True,
        overview_enabled=True,
        retain_automatic=True,
        retain_search_history=True,
    )
    beliefs = [
        memory(belief_id=600 + i, statement=f"Iris fact {i}").model_copy(
            update={"subject": f"Iris subject {i}"}
        )
        for i in range(belief_count)
    ]
    async with factory() as uow:
        for belief in beliefs:
            await uow.memories.upsert_belief(belief)
    person = UUID(int=900)
    contexts = [
        TracedPersonContext(
            record_id=UUID(int=901 + i),
            revision=1,
            person_ids=[person],
            kind="person" if i == 0 else extra_kind,
            text="Iris" if i == 0 else "Received email",
            sensitivity=Sensitivity.INTERNAL,
        )
        for i in range(3)
    ]
    query = tool.query(
        {"text": "Iris"},
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        current_scope="project-a",
    )
    result = await retriever.recall(query, session_id=SESSION_ID, people_items=contexts)
    assert {item.belief_id for item in result.items} == {belief.id for belief in beliefs}
    assert contexts[0] in result.people, "Identity context must survive the fact-first allocation"
    assert any(item.kind == extra_kind for item in result.people)
    if belief_count == 1:
        assert result.people == contexts, "Spare capacity must still carry recent interactions"
    assert len(result.items) + len(result.people) <= query.max_items
    assert result.tokens <= query.budget_tokens


@pytest.mark.parametrize("role", ["father", "accountant", "coach", "emergency contact"])
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize(
    "restriction",
    [None, "ambiguous", "private", "expired", "unresolved", "indirect", "excluded", "permission"],
)
async def test_relationship_reference_retrieves_linked_facts_without_a_name(
    role: str, restriction: str | None, focused: bool
) -> None:
    clock, factory = await memory_uow_factory()
    await relationship_recall_contract(clock, factory, role, restriction, focused=focused)


async def test_relationship_links_survive_more_recent_topical_distractors() -> None:
    clock, factory = await memory_uow_factory()
    await relationship_recall_contract(clock, factory, "coach", None, distractors=256)


@pytest.mark.parametrize("role", ["portfolio CEO", "tennis coach", "estate accountant"])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("focused", [True, False])
async def test_role_discovery_rejects_shared_topic_words(
    role: str, partial: bool, focused: bool
) -> None:
    clock, factory = await memory_uow_factory()
    await qualified_relationship_recall_contract(clock, factory, role, partial, focused=focused)


@pytest.mark.parametrize("case", ["role_word", "stored_label", "shared_word"])
async def test_an_unnamed_tie_selects_no_person_and_hides_no_fact(case: str) -> None:
    clock, factory = await memory_uow_factory()
    await unnamed_tie_recall_contract(clock, factory, case)


# Each case: the ties as (name, owner is the subject, predicate, qualifier), the
# role word the question uses, and the one person it names (None for nobody).
# `subject --predicate--> object` says the subject is the object's predicate:
# owner --parent--> Ava makes Ava the owner's child (ADR-0259).
_OwnerTie = tuple[str, bool, str, str]
OWNER_SIDE_LABEL_CASES: dict[str, tuple[tuple[_OwnerTie, ...], str, str | None]] = {
    # Both ties formed owner-first: the owner is Ava's parent and Noor's child.
    "owner_first_parent": (
        (("Ava", True, "parent", ""), ("Noor", True, "child", "")),
        "parent",
        "Noor",
    ),
    "owner_first_child": (
        (("Ava", True, "parent", ""), ("Noor", True, "child", "")),
        "child",
        "Ava",
    ),
    # The shape `agent people link-kinship` writes: one predicate, two directions.
    "linked_kinship_parent": (
        (("Ava", True, "parent", ""), ("Noor", False, "parent", "")),
        "parent",
        "Noor",
    ),
    "linked_kinship_child": (
        (("Ava", True, "parent", ""), ("Noor", False, "parent", "")),
        "child",
        "Ava",
    ),
    "person_first_child": ((("Ava", False, "child", ""),), "child", "Ava"),
    # The owner invests in Pat; no stored word says what that makes Pat.
    "owner_first_one_way": ((("Pat", True, "investor", ""),), "investor", None),
    "person_first_one_way": ((("Pat", False, "investor", ""),), "investor", "Pat"),
    "owner_first_mutual": ((("Quinn", True, "sibling", ""),), "sibling", "Quinn"),
    # A qualifier already states the person's role and stays the whole label.
    "qualified_by_its_qualifier": ((("Ava", True, "parent", "daughter"),), "daughter", "Ava"),
    "qualified_not_by_predicate": ((("Ava", True, "parent", "daughter"),), "child", None),
}


@pytest.mark.parametrize("case", sorted(OWNER_SIDE_LABEL_CASES))
async def test_an_unqualified_tie_is_named_by_the_persons_role_toward_the_owner(case: str) -> None:
    clock, factory = await memory_uow_factory()
    await owner_side_label_recall_contract(clock, factory, case)


@pytest.mark.parametrize("inactive_label", ["expired", "future"])
async def test_focused_role_label_must_be_current(inactive_label: str) -> None:
    clock, factory = await memory_uow_factory()
    await qualified_relationship_recall_contract(
        clock, factory, "portfolio CEO", True, inactive_label=inactive_label
    )


async def qualified_relationship_recall_contract(
    clock: FixedClock,
    factory: UnitOfWorkFactory,
    role: str,
    partial: bool,
    *,
    inactive_label: str | None = None,
    focused: bool = True,
) -> None:
    """Exercise complete role labels and temporal validity for every relevance recall."""
    await relationship_recall_contract(
        clock,
        factory,
        role,
        "partial_role" if partial else None,
        query_text=f"Review my {role.split()[0]} plans."
        if partial
        else f"Choose a gift for my {role}.",
        focused=focused,
        inactive_label=inactive_label,
    )


async def unnamed_tie_recall_contract(
    clock: FixedClock, factory: UnitOfWorkFactory, case: str
) -> None:
    """A tie the question does not name selects nobody, so no linked fact is hidden (ADR-0348).

    `role_word`: a relative known only as "mom" sits beside the named mother; the
    word is that record's unconfirmed role, not a name. `stored_label`: the same
    directory, asked by the label the named mother's tie stores. `shared_word`:
    two relatives' ties share a topic word with the question and neither is named.
    """
    from agent_core.domain.memory import BeliefType
    from agent_core.domain.people import (
        PeopleEndpoint,
        PersonIdentifier,
        RelationshipAssertion,
        RelationshipPredicate,
    )
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE

    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }

    def belief(key: int, subject: str, statement: str, kind: BeliefType) -> MemoryRecord:
        return memory(belief_id=key, statement=statement).model_copy(
            update={"subject": subject, "belief_type": kind, "store_position": key - 900}
        )

    relationship, attribute = BeliefType.RELATIONSHIP, BeliefType.USER_MODEL_ATTR
    ties: list[tuple[Person, MemoryRecord, RelationshipPredicate, str]]
    if case == "shared_word":
        named = Person(id=uuid4(), display_name="Rae", **common)
        other = Person(id=uuid4(), display_name="Quinn", **common)
        ties = [
            (
                named,
                belief(901, "Rae", "Rae is the user's wife and loves weekend hikes.", relationship),
                "spouse",
                "",
            ),
            (
                other,
                belief(
                    902,
                    "Quinn",
                    "Quinn is the user's brother and joins weekend hikes.",
                    relationship,
                ),
                "sibling",
                "",
            ),
        ]
        wanted = belief(903, "Rae", "Rae is training for a weekend trail race.", attribute)
        text = "Plan something for the weekend."
    else:
        named = Person(id=uuid4(), display_name="Marta", **common)
        other = Person(id=uuid4(), display_name="mom", state="provisional", **common)
        ties = [
            (
                named,
                belief(901, "Marta", "Marta is the user's mother.", relationship),
                "parent",
                "mother",
            ),
            (
                other,
                belief(902, "User", "User's mom is visiting in June.", relationship),
                "parent",
                "",
            ),
        ]
        wanted = belief(903, "Marta", "Marta lives in a retirement village in Lisbon.", attribute)
        text = f"Where does my {'mom' if case == 'role_word' else 'mother'} live?"
    async with factory() as uow:
        for person, tie, predicate, qualifier in ties:
            await uow.people.put(person, expected_revision=0)
            await uow.memories.upsert_belief(tie)
            await uow.people.put(
                PersonMemoryLink(id=uuid4(), person_id=person.id, belief_id=tie.id, **common),
                expected_revision=0,
            )
            owner_first = predicate == "spouse"
            await uow.people.put(
                RelationshipAssertion(
                    id=uuid4(),
                    subject=PeopleEndpoint(kind="owner")
                    if owner_first
                    else PeopleEndpoint(kind="person", id=person.id),
                    object=PeopleEndpoint(kind="person", id=person.id)
                    if owner_first
                    else PeopleEndpoint(kind="owner"),
                    predicate=predicate,
                    qualifier=qualifier,
                    belief_id=tie.id,
                    **common,
                ),
                expected_revision=0,
            )
        if case != "shared_word":
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=other.id,
                    identifier_kind="role",
                    namespace="owner",
                    value="mom",
                    context="owner",
                    verification="contextual",
                    valid_from=NOW,
                    **common,
                ),
                expected_revision=0,
            )
        await uow.memories.upsert_belief(wanted)
        await uow.people.put(
            PersonMemoryLink(id=uuid4(), person_id=named.id, belief_id=wanted.id, **common),
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
    query = recall_query(text=text, budget_tokens=2000).model_copy(
        update={"candidate_selection": "relevance"}
    )
    result = await service.automatic_recall(owner, query, session_id=SESSION_ID)
    assert wanted.id in {item.belief_id for item in result.items}
    focal = {person_id for item in result.people for person_id in item.person_ids}
    assert other.id not in focal
    assert (named.id in focal) == (case == "stored_label")


async def owner_side_label_recall_contract(
    clock: FixedClock, factory: UnitOfWorkFactory, case: str
) -> None:
    """An unqualified tie is named by the person's role toward the owner (ADR-0348).

    The stored predicate is the subject's role, so an owner-first edge names its
    person by the inverse: a child for `parent`, a parent for `child`, the same
    word for a mutual tie, and nothing for a one-way tie that has no inverse.
    """
    from typing import cast

    from agent_core.domain.memory import BeliefType
    from agent_core.domain.people import (
        PeopleEndpoint,
        RelationshipAssertion,
        RelationshipPredicate,
    )
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE

    ties, role, expected = OWNER_SIDE_LABEL_CASES[case]
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    people: dict[str, Person] = {}
    facts: dict[str, MemoryRecord] = {}
    async with factory() as uow:
        for index, (name, owner_first, predicate, qualifier) in enumerate(ties):
            person = people[name] = Person(id=uuid4(), display_name=name, **common)
            # Every tie shares the question's topic word, so each proposes its person.
            tie = memory(
                belief_id=901 + 2 * index, statement=f"{name} and the user trade a gift each year."
            ).model_copy(
                update={
                    "subject": name,
                    "belief_type": BeliefType.RELATIONSHIP,
                    "store_position": 1 + 2 * index,
                }
            )
            fact = facts[name] = memory(
                belief_id=902 + 2 * index, statement=f"{name} would love a gift of rare tea."
            ).model_copy(
                update={
                    "subject": name,
                    "belief_type": BeliefType.USER_MODEL_ATTR,
                    "store_position": 2 + 2 * index,
                }
            )
            await uow.people.put(person, expected_revision=0)
            for belief in (tie, fact):
                await uow.memories.upsert_belief(belief)
                await uow.people.put(
                    PersonMemoryLink(
                        id=uuid4(), person_id=person.id, belief_id=belief.id, **common
                    ),
                    expected_revision=0,
                )
            ends = (PeopleEndpoint(kind="owner"), PeopleEndpoint(kind="person", id=person.id))
            subject, object_ = ends if owner_first else ends[::-1]
            await uow.people.put(
                RelationshipAssertion(
                    id=uuid4(),
                    subject=subject,
                    object=object_,
                    predicate=cast(RelationshipPredicate, predicate),
                    qualifier=qualifier,
                    belief_id=tie.id,
                    **common,
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
    query = recall_query(text=f"Choose a gift for my {role}.", budget_tokens=2000).model_copy(
        update={"candidate_selection": "relevance"}
    )
    result = await service.automatic_recall(owner, query, session_id=SESSION_ID)
    focal = {person_id for item in result.people for person_id in item.person_ids}
    recalled = {item.belief_id for item in result.items}
    if expected is None:
        # Nobody is named: ordinary recall runs and hides no person-linked fact.
        assert focal == set()
        assert {fact.id for fact in facts.values()} <= recalled
        return
    assert focal == {people[expected].id}
    assert facts[expected].id in recalled


async def relationship_recall_contract(
    clock: FixedClock,
    factory: UnitOfWorkFactory,
    role: str,
    restriction: str | None,
    *,
    distractors: int = 0,
    query_text: str | None = None,
    focused: bool = False,
    inactive_label: str | None = None,
) -> None:
    """Exercise governed relationship discovery against either persistence adapter."""
    from datetime import timedelta

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
        if inactive_label is not None:
            await uow.people.put(
                edge.model_copy(
                    update={
                        "id": uuid4(),
                        "qualifier": role.split()[0],
                        "valid_to": NOW if inactive_label == "expired" else None,
                        "valid_from": NOW + timedelta(days=1)
                        if inactive_label == "future"
                        else None,
                    }
                ),
                expected_revision=0,
            )
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
        text=query_text or f"Choose an exhibition for my {role}.", budget_tokens=2000
    ).model_copy(
        update={
            "candidate_selection": "relevance",
            "focused_people_context": focused,
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
    from agent_core.domain.knowledge import (
        KnowledgeQuery,
        KnowledgeSemanticQuery,
        RetrievedPassage,
    )
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
        self: InMemoryKnowledgeStore,
        query: KnowledgeQuery,
        *,
        semantic: KnowledgeSemanticQuery | None = None,
    ) -> list[RetrievedPassage]:
        result = await original_search(self, query, semantic=semantic)
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


@pytest.mark.parametrize("interactions", [False, True])
@pytest.mark.parametrize("focused_people", [False, True])
async def test_overview_first_person_uses_full_budget_before_competing_beliefs(
    interactions: bool, focused_people: bool
) -> None:
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
            kind="interaction" if interactions else "relationship",
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
            "focused_people_context": focused_people,
        }
    )
    result = await retriever.recall(query, session_id=SESSION_ID, people_items=contexts)
    assert result.items, "Competing ordinary beliefs must actually be eligible"
    assert all(str(item.record_id) in result.rendered for item in contexts[:9])
    assert "Incomplete context" not in result.rendered
    assert result.tokens <= query.budget_tokens


@pytest.mark.parametrize("hard_exclusion", [False, True])
async def test_context_allocation_preserves_identity_discovery(hard_exclusion: bool) -> None:
    """Verify context allocation preserves identity discovery."""
    from agent_core.context.recall_budget import AUTOMATIC_RESERVE, select_search_results
    from agent_core.domain.memory import BeliefType
    from agent_core.domain.people import PeopleEndpoint, RelationshipAssertion
    from tests.unit.test_context_memory_budget import search_result

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    first = Person(id=uuid4(), display_name="Quinn", **common)
    second = Person(id=uuid4(), display_name="Morgan", **common)
    records = [
        memory(belief_id=5000 + i, statement=f"The user's coach handles activity {i}.").model_copy(
            update={"belief_type": BeliefType.RELATIONSHIP}
        )
        for i in range(7)
    ]
    async with factory() as uow:
        for person in (first, second):
            await uow.people.put(person, expected_revision=0)
        for i, record in enumerate(records):
            await uow.memories.upsert_belief(record)
            await uow.people.put(
                RelationshipAssertion(
                    id=uuid4(),
                    subject=PeopleEndpoint(kind="person", id=second.id if i == 6 else first.id),
                    object=PeopleEndpoint(kind="owner"),
                    predicate="other",
                    qualifier="coach",
                    belief_id=record.id,
                    **common,
                ),
                expected_revision=0,
            )
    query = recall_query(text="What does my coach recommend?", max_items=20).model_copy(
        update={
            "candidate_selection": "relevance",
            "exclude_ids": (records[-1].id,) if hard_exclusion else (),
        }
    )
    call, result = search_result("earlier", text="Already shown", tokens=1500, items=15)
    assert result.memory_recall is not None
    result.memory_recall = result.memory_recall.model_copy(
        update={
            "returned_ids": (records[0].id if hard_exclusion else records[-1].id,),
        }
    )
    _, allowance, _ = select_search_results(
        [call, result],
        reserve=AUTOMATIC_RESERVE,
        permitted_receipts={result.memory_recall},
    )
    bounded = allowance.bound(query)
    assert bounded is not None
    service = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    async with factory() as uow:
        before = await service._relationship_people(uow, owner, query, Sensitivity.SENSITIVE)
        after = await service._relationship_people(uow, owner, bounded, Sensitivity.SENSITIVE)
    assert before[0] == ([first.id] if hard_exclusion else [])
    assert after[0] == before[0], "Presentation must not turn ambiguous evidence into one identity"
