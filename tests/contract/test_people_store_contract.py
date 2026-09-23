"""Shared People repository behavior, reused by real PostgreSQL tests."""

from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.people import InMemoryPeopleStore
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    PeopleQuery,
    PeopleRecord,
    PeopleSource,
    Person,
    PersonIdentifier,
)
from agent_core.ports.people import PeopleStore
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, principal


async def people_directory_contract(store: PeopleStore) -> None:
    owner = principal()
    people = []
    directory_rows: list[tuple[Literal["provisional", "active"], bool, Sensitivity]] = [
        ("provisional", True, Sensitivity.SENSITIVE),
        ("active", False, Sensitivity.SENSITIVE),
        ("active", True, Sensitivity.RESTRICTED),
        ("active", True, Sensitivity.SENSITIVE),
        ("active", True, Sensitivity.SENSITIVE),
    ]
    for state, pinned, sensitivity in directory_rows:
        person = Person(
            id=uuid4(),
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            display_name="Directory contract",
            state=state,
            pinned=pinned,
            sensitivity=sensitivity,
            created_at=NOW,
            updated_at=NOW,
        )
        await store.put(person, expected_revision=0)
        people.append(person)
    query = PeopleQuery(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        text="Directory contract",
        states=["active"],
        pinned=True,
        sensitivity_ceiling=Sensitivity.SENSITIVE,
        limit=1,
    )
    first = await store.query(query)
    assert {row.id for row in first} == {row.id for row in people[-2:]}
    second = await store.query(query.model_copy(update={"after": first[0].id}))
    assert [row.id for row in second] == [first[1].id]


async def test_in_memory_people_directory_contract() -> None:
    await people_directory_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_store_contract(store: PeopleStore) -> None:
    owner = principal()
    foreign = owner.model_copy(update={"principal_id": "other-person"})
    person = Person(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        display_name="Alex",
        created_at=NOW,
        updated_at=NOW,
    )
    assert await store.put(person, expected_revision=0) == person
    same_name = person.model_copy(update={"id": uuid4()})
    await store.put(same_name, expected_revision=0)
    query = PeopleQuery(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        sensitivity_ceiling=Sensitivity.SENSITIVE,
    )
    assert {r.id for r in await store.query(query)} == {person.id, same_name.id}
    assert await store.get(foreign, person.id, ceiling=Sensitivity.RESTRICTED) is None
    assert await store.get(owner, person.id, ceiling=Sensitivity.INTERNAL) is None
    assert (
        await store.query(query.model_copy(update={"sensitivity_ceiling": Sensitivity.INTERNAL}))
        == []
    )
    with pytest.raises(ConflictError):
        await store.put(person, expected_revision=0)
    updated = person.model_copy(
        update={
            "display_name": "Alex Rivera",
            "revision": 2,
            "updated_at": NOW + timedelta(days=1),
        }
    )
    await store.put(updated, expected_revision=1)
    assert await store.get(owner, person.id, ceiling=Sensitivity.SENSITIVE) == updated
    assert await store.get(owner, person.id, ceiling=Sensitivity.SENSITIVE, known_at=NOW) == person
    assert await store.erase(foreign, [person.id]) == 0
    assert await store.erase(owner, [person.id]) == 1
    assert await store.get(owner, person.id, ceiling=Sensitivity.SENSITIVE, known_at=NOW) is None
    with pytest.raises(ConflictError):
        await store.put(person, expected_revision=0)
    assert await store.erase_principal(owner) >= 1
    assert await store.query(query) == []


async def test_memory_people_store_contract() -> None:
    await people_store_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_source_visibility_contract(store: PeopleStore) -> None:
    owner = principal()
    person = Person(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        display_name="Sam",
        created_at=NOW,
        updated_at=NOW,
    )
    source = PeopleSource(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=uuid4(),
        event_sequence=1,
        source_kind="owner",
        source_revision="event@1",
        evidence_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    await store.put(person, expected_revision=0)
    await store.put(source, expected_revision=0)
    identifier = PersonIdentifier(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        person_id=person.id,
        identifier_kind="email",
        namespace="personal",
        value="sam@example.test",
        verification="channel_observed",
        support_ids=[source.id],
        created_at=NOW,
        updated_at=NOW,
        valid_from=NOW,
    )
    await store.put(identifier, expected_revision=0)
    assert await store.get(owner, identifier.id, ceiling=Sensitivity.SENSITIVE) == identifier
    await store.put(
        source.model_copy(
            update={
                "revision": 2,
                "sensitivity": Sensitivity.RESTRICTED,
                "updated_at": NOW + timedelta(days=1),
            }
        ),
        expected_revision=1,
    )
    assert (
        await store.get(owner, identifier.id, ceiling=Sensitivity.SENSITIVE, known_at=NOW) is None
    )
    assert await store.get(owner, source.id, ceiling=Sensitivity.SENSITIVE, known_at=NOW) is None
    await store.erase(owner, [source.id])
    assert (
        await store.get(owner, identifier.id, ceiling=Sensitivity.RESTRICTED, known_at=NOW) is None
    )
    with pytest.raises(ConflictError):
        await store.put(identifier.model_copy(update={"id": uuid4()}), expected_revision=0)
    # Erasure physically removes every derived revision, not only read visibility.
    assert await store.erase(owner, [identifier.id]) == 0


async def test_memory_people_source_visibility_contract() -> None:
    await people_source_visibility_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def test_session_erasure_removes_people_derivations() -> None:
    from tests.contract.support import memory_uow_factory, session

    _clock, factory = await memory_uow_factory()
    owner = principal()
    source = PeopleSource(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=session().id,
        event_sequence=1,
        source_kind="owner",
        evidence_at=NOW,
        source_revision="event@1",
        created_at=NOW,
        updated_at=NOW,
    )
    person = Person(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        display_name="Alex",
        support_ids=[source.id],
        created_at=NOW,
        updated_at=NOW,
    )
    async with factory() as uow:
        await uow.people.put(source, expected_revision=0)
        await uow.people.put(person, expected_revision=0)
        await uow.session_deletions.delete(session().id, owner, NOW)
        assert await uow.people.get(owner, person.id, ceiling=Sensitivity.RESTRICTED) is None
        assert await uow.people.erase(owner, [person.id, source.id]) == 0


async def test_relationship_cannot_reference_a_missing_organization() -> None:
    from agent_core.domain.people import PeopleEndpoint, RelationshipAssertion

    owner = principal()
    store = InMemoryPeopleStore(FixedClock(NOW))
    relation = RelationshipAssertion(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
        subject=PeopleEndpoint(kind="owner"),
        object=PeopleEndpoint(kind="organization", id=uuid4()),
        predicate="employment",
        belief_id=uuid4(),
    )
    with pytest.raises(ConflictError):
        await store.put(relation, expected_revision=0)


async def people_transitive_visibility_contract(store: PeopleStore) -> None:
    owner = principal()
    source = PeopleSource(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=uuid4(),
        event_sequence=1,
        source_kind="owner",
        evidence_at=NOW,
        source_revision="event@1",
        created_at=NOW,
        updated_at=NOW,
    )
    person = Person(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        display_name="Alex",
        support_ids=[source.id],
        created_at=NOW,
        updated_at=NOW,
    )
    alias = PersonIdentifier(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        person_id=person.id,
        identifier_kind="name",
        namespace="owner",
        value="Al",
        verification="owner_confirmed",
        valid_from=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    for row in (source, person, alias):
        await store.put(row, expected_revision=0)
    await store.put(
        source.model_copy(
            update={
                "revision": 2,
                "sensitivity": Sensitivity.RESTRICTED,
                "updated_at": NOW + timedelta(days=1),
            }
        ),
        expected_revision=1,
    )
    assert await store.get(owner, person.id, ceiling=Sensitivity.SENSITIVE) is None
    assert await store.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE) is None
    assert (
        await store.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                kinds=["identifier"],
                text="Al",
                sensitivity_ceiling=Sensitivity.SENSITIVE,
            )
        )
        == []
    )


async def test_people_transitive_visibility() -> None:
    await people_transitive_visibility_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_history_paging_contract(store: PeopleStore) -> None:
    from agent_core.domain.people import InteractionParticipant, PeopleInteraction

    owner = principal()
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Sam", **common)
    await store.put(person, expected_revision=0)
    rows = [
        PeopleInteraction(
            id=uuid4(),
            channel="chat",
            interaction_kind="meeting",
            attribution="owner_reported",
            direction="reported",
            summary=f"Meeting {i}",
            participants=[InteractionParticipant(person_id=person.id, role="participant")],
            occurred_at=NOW - timedelta(days=i) if i < 3 else None,
            **common,
        )
        for i in range(4)
    ]
    for row in rows:
        await store.put(row, expected_revision=0)
    query = PeopleQuery(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        person_id=person.id,
        kinds=["interaction"],
        sensitivity_ceiling=Sensitivity.SENSITIVE,
        sort="history",
        limit=2,
    )
    first = await store.query(query)
    assert first == rows[:3]
    assert isinstance(first[1], PeopleInteraction)
    second = await store.query(
        query.model_copy(update={"after": first[1].id, "after_event_at": first[1].occurred_at})
    )
    assert second == rows[2:]
    assert await store.query(query.model_copy(update={"unknown_time": "only"})) == rows[3:]
    assert (
        await store.query(query.model_copy(update={"since": NOW - timedelta(hours=1)})) == rows[:1]
    )


async def test_memory_people_history_paging_contract() -> None:
    await people_history_paging_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def independent_people_support_contract(store: PeopleStore) -> None:
    owner = principal()
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    sources = [
        PeopleSource(
            id=uuid4(),
            session_id=uuid4(),
            event_sequence=1,
            source_kind="owner",
            source_revision="owner@1",
            evidence_at=NOW,
            **common,
        )
        for _ in range(2)
    ]
    person = Person(id=uuid4(), display_name="Sam", support_ids=[sources[0].id], **common)
    confirmed = person.model_copy(
        update={
            "revision": 2,
            "updated_at": NOW + timedelta(days=1),
            "support_ids": [sources[1].id],
        }
    )
    for source in sources:
        await store.put(source, expected_revision=0)
    await store.put(person, expected_revision=0)
    await store.put(confirmed, expected_revision=1)
    await store.erase_session(owner, sources[0].session_id)
    assert await store.get(owner, person.id, ceiling=Sensitivity.SENSITIVE) == confirmed
    assert await store.get(owner, person.id, ceiling=Sensitivity.SENSITIVE, known_at=NOW) is None


async def test_memory_people_retains_independent_support() -> None:
    await independent_people_support_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_email_erasure_contract(store: PeopleStore) -> None:
    owner = principal()
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    sources = [
        PeopleSource(
            id=uuid4(),
            session_id=uuid4(),
            event_sequence=1,
            source_kind="email",
            account_id=account,
            thread_id="thread",
            message_id="message",
            source_revision="email@1",
            evidence_at=NOW,
            **common,
        )
        for account in ("work", "personal")
    ]
    people = [
        Person(id=uuid4(), display_name="Sam", support_ids=[source.id], **common)
        for source in sources
    ]
    for source, person in zip(sources, people, strict=True):
        await store.put(source, expected_revision=0)
        await store.put(person, expected_revision=0)
    assert await store.erase_email_source(owner, "work", "thread", frozenset({"other"})) == 0
    assert await store.erase_email_source(owner, "work", "thread", frozenset({"message"})) == 2
    assert await store.get(owner, people[0].id, ceiling=Sensitivity.SENSITIVE) is None
    assert await store.get(owner, people[1].id, ceiling=Sensitivity.SENSITIVE) == people[1]


async def test_memory_people_email_erasure_contract() -> None:
    await people_email_erasure_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def test_email_learning_erasure_reaches_people_and_cross_session_traces() -> None:
    from agent_core.domain.memory import TracedPersonContext
    from tests.contract.memory_fixtures import trace
    from tests.contract.support import memory_uow_factory, session

    _, factory = await memory_uow_factory()
    owner = principal()
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    source = PeopleSource(
        id=uuid4(),
        session_id=session().id,
        event_sequence=1,
        source_kind="email",
        account_id="work",
        thread_id="thread",
        message_id="message",
        source_revision="email@1",
        evidence_at=NOW,
        **common,
    )
    person = Person(id=uuid4(), display_name="Sam", support_ids=[source.id], **common)
    recall = trace().model_copy(
        update={
            "people": [
                TracedPersonContext(
                    record_id=person.id,
                    revision=1,
                    person_ids=[person.id],
                    kind="person",
                    text="Sam",
                    sensitivity=Sensitivity.SENSITIVE,
                    source_ids=[source.id],
                )
            ]
        }
    )
    async with factory() as uow:
        await uow.people.put(source, expected_revision=0)
        await uow.people.put(person, expected_revision=0)
        await uow.traces.record(recall)
        await uow.session_deletions.erase_email_source(
            owner, "work", "thread", frozenset({"message"}), NOW
        )
        assert await uow.people.get(owner, person.id, ceiling=Sensitivity.SENSITIVE) is None
        from agent_core.domain.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await uow.traces.get(recall.id, owner)


async def people_recent_directory_contract(store: PeopleStore) -> None:
    from uuid import UUID

    from agent_core.domain.people import InteractionParticipant, PeopleInteraction

    owner = principal()
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    people = [Person(id=UUID(int=9100 + i), display_name=f"Recent {i}", **common) for i in range(4)]
    for person in people:
        await store.put(person, expected_revision=0)
    for i, days, sensitivity in [
        (0, 1, Sensitivity.SENSITIVE),
        (1, 3, Sensitivity.SENSITIVE),
        (0, 4, Sensitivity.RESTRICTED),
        (2, 10, Sensitivity.SENSITIVE),
    ]:
        await store.put(
            PeopleInteraction(
                id=uuid4(),
                channel="chat",
                interaction_kind="meeting",
                direction="reported",
                attribution="owner_reported",
                summary="Recorded meeting",
                participants=[InteractionParticipant(person_id=people[i].id, role="participant")],
                occurred_at=NOW + timedelta(days=days),
                sensitivity=sensitivity,
                **common,
            ),
            expected_revision=0,
        )
    query = PeopleQuery.model_validate(
        {
            "tenant_id": owner.tenant_id,
            "principal_id": owner.principal_id,
            "kinds": ["person"],
            "sensitivity_ceiling": "sensitive",
            "text": "Recent",
            "sort": "recent",
            "as_of": NOW + timedelta(days=5),
            "limit": 1,
        }
    )
    page = await store.query(query)
    assert [row.id for row in page] == [people[1].id, people[0].id]
    second = await store.query(query.model_copy(update={"after": people[1].id}))
    assert [row.id for row in second] == [people[0].id, people[2].id]
    third = await store.query(query.model_copy(update={"after": people[0].id}))
    assert [row.id for row in third] == [people[2].id, people[3].id]


async def test_recent_people_directory_uses_visible_interactions_before_paging() -> None:
    await people_recent_directory_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_duplicate_source_erasure_contract(store: PeopleStore) -> None:
    from uuid import UUID

    from agent_core.domain.people import InteractionParticipant, PeopleInteraction, PersonMemoryLink

    owner = principal()
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    sources = [
        PeopleSource(
            id=uuid4(),
            session_id=uuid4(),
            event_sequence=1,
            source_kind="email",
            evidence_at=NOW,
            account_id=account,
            thread_id="copied",
            message_id="message",
            source_revision="verified",
            copy_group="d" * 64,
            **common,
        )
        for account in ["first", "second"]
    ]
    person = Person(
        id=uuid4(), display_name="Duplicate source", support_ids=[sources[0].id], **common
    )
    history = PeopleInteraction(
        id=uuid4(),
        channel="email",
        interaction_kind="exchange",
        attribution="observed",
        direction="incoming",
        summary="Received email",
        occurred_at=NOW,
        participants=[InteractionParticipant(person_id=person.id, role="sender")],
        support_ids=[s.id for s in sources],
        **common,
    )
    claim = PersonMemoryLink(
        id=uuid4(),
        person_id=person.id,
        belief_id=UUID(int=9199),
        support_ids=[s.id for s in sources],
        **common,
    )
    records: list[PeopleRecord] = [*sources, person, history, claim]
    for row in records:
        await store.put(row, expected_revision=0)
    await store.erase_email_source(owner, "first", "copied", frozenset(["message"]))
    retained = await store.get(owner, history.id, ceiling=Sensitivity.SENSITIVE)
    assert retained is not None and retained.support_ids == [sources[1].id]
    assert await store.get(owner, claim.id, ceiling=Sensitivity.SENSITIVE) is None
    assert await store.get(owner, history.id, ceiling=Sensitivity.SENSITIVE, known_at=NOW) is None
    await store.erase_email_source(owner, "second", "copied", frozenset(["message"]))
    assert await store.get(owner, history.id, ceiling=Sensitivity.SENSITIVE) is None
    assert await store.get(owner, person.id, ceiling=Sensitivity.SENSITIVE) is None


async def test_complete_duplicate_sources_preserve_metadata_but_not_composite_claims() -> None:
    await people_duplicate_source_erasure_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_interaction_redirect_contract(store: PeopleStore) -> None:
    from uuid import UUID

    from agent_core.domain.people import InteractionParticipant, PeopleInteraction

    owner = principal()
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Email copy fixture", **common)
    await store.put(person, expected_revision=0)
    original = PeopleInteraction(
        id=UUID(int=13001),
        channel="email",
        interaction_kind="exchange",
        attribution="observed",
        direction="incoming",
        summary="Received email",
        participants=[InteractionParticipant(person_id=person.id, role="sender")],
        occurred_at=NOW,
        **common,
    )
    other = original.model_copy(update={"id": UUID(int=13003)})
    canonical = original.model_copy(
        update={
            "id": UUID(int=13002),
            "created_at": NOW + timedelta(days=1),
            "updated_at": NOW + timedelta(days=1),
        }
    )
    for row in [original, other, canonical]:
        await store.put(row, expected_revision=0)
    redirected = original.model_copy(
        update={"revision": 2, "superseded_by": canonical.id, "updated_at": NOW + timedelta(days=1)}
    )
    await store.put(redirected, expected_revision=1)
    query = PeopleQuery(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        person_id=person.id,
        kinds=["interaction"],
        limit=1,
        sensitivity_ceiling=Sensitivity.SENSITIVE,
    )
    assert await store.query(query) == [canonical, other]
    assert await store.query(query.model_copy(update={"after": canonical.id})) == [other]
    assert await store.query(query.model_copy(update={"known_at": NOW})) == [original, other]
    assert await store.query(
        query.model_copy(update={"include_superseded": True, "limit": 100})
    ) == [redirected, canonical, other]
    await store.put(
        canonical.model_copy(update={"revision": 2, "sensitivity": Sensitivity.RESTRICTED}),
        expected_revision=1,
    )
    assert await store.get(owner, original.id, ceiling=Sensitivity.SENSITIVE) is None
    assert await store.query(query) == [other]


async def test_interaction_redirects_preserve_history_and_filter_before_pagination() -> None:
    await people_interaction_redirect_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_erasure_fence_contract(store: PeopleStore) -> None:
    """Current privacy wins over historical reads throughout bounded cleanup."""
    owner = principal()
    foreign = owner.model_copy(update={"principal_id": "another-owner"})
    person = Person(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        display_name="Sam",
        created_at=NOW,
        updated_at=NOW,
    )
    survivor = person.model_copy(update={"id": uuid4(), "display_name": "Alex"})
    await store.put(person, expected_revision=0)
    await store.put(survivor, expected_revision=0)
    updated = person.model_copy(
        update={
            "revision": 2,
            "display_name": "Sam Taylor",
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    await store.put(updated, expected_revision=1)
    assert await store.fence_for_erasure(foreign, [person.id]) == 0
    assert await store.fence_for_erasure(owner, [person.id]) == 1
    assert await store.fence_for_erasure(owner, [person.id]) == 0
    assert await store.get(owner, person.id, ceiling=Sensitivity.RESTRICTED) is None
    assert await store.get(owner, person.id, ceiling=Sensitivity.RESTRICTED, known_at=NOW) is None
    with pytest.raises(ConflictError):
        await store.put(updated.model_copy(update={"revision": 3}), expected_revision=2)
    assert not await store.purge_erased(foreign, limit=1)
    assert await store.purge_erased(owner, limit=1)
    assert not await store.purge_erased(owner, limit=1)
    assert not await store.purge_erased(owner, limit=1)
    assert await store.get(owner, survivor.id, ceiling=Sensitivity.RESTRICTED) == survivor
    with pytest.raises(ConflictError):
        await store.put(person, expected_revision=0)
    with pytest.raises(ValueError):
        await store.purge_erased(owner, limit=257)


async def test_people_fence_is_immediate_and_cleanup_is_bounded() -> None:
    await people_erasure_fence_contract(InMemoryPeopleStore(FixedClock(NOW)))


def _admission_fields() -> PeopleFields:
    owner = principal()
    return {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }


async def people_preserving_erase_contract(store: PeopleStore) -> None:
    """Erasing one person keeps records that no longer depend on it (ADR-0121).

    The directory repair detaches mentions and endpoint identifiers, drops the
    person from shared interactions, and then erases what still depends on the
    person. A plain erase follows every historical revision's links and would
    also destroy the detached rows; the preserving erase follows current heads
    only and strips the survivors' stale revisions.
    """
    from agent_core.domain.people import (
        InteractionParticipant,
        PeopleEndpoint,
        PeopleInteraction,
        PersonMemoryLink,
        PersonMention,
        RelationshipAssertion,
    )

    owner = principal()
    common = _admission_fields()
    later: PeopleFields = {**common, "updated_at": NOW + timedelta(days=1)}

    async def fixture() -> dict[str, PeopleRecord]:
        source = PeopleSource(
            id=uuid4(),
            session_id=uuid4(),
            event_sequence=1,
            source_kind="owner",
            source_revision="event@1",
            evidence_at=NOW,
            **common,
        )
        pruned = Person(id=uuid4(), display_name="Pruned stranger", **common)
        kept = Person(id=uuid4(), display_name="Kept correspondent", **common)
        for row in (source, pruned, kept):
            await store.put(row, expected_revision=0)
        identifier = PersonIdentifier(
            id=uuid4(),
            person_id=pruned.id,
            identifier_kind="email",
            namespace="owner",
            value=f"stranger-{uuid4().hex[:8]}@example.test",
            context="owner",
            verification="channel_observed",
            valid_from=NOW,
            support_ids=[source.id],
            **common,
        )
        mention = PersonMention(
            id=uuid4(),
            source_id=source.id,
            person_id=pruned.id,
            start=0,
            end=7,
            support_ids=[source.id],
            **common,
        )
        shared = PeopleInteraction(
            id=uuid4(),
            channel="email",
            interaction_kind="exchange",
            attribution="observed",
            direction="incoming",
            summary="Received email",
            occurred_at=NOW,
            participants=[
                InteractionParticipant(person_id=pruned.id, role="sender"),
                InteractionParticipant(person_id=kept.id, role="recipient"),
            ],
            **common,
        )
        solo = shared.model_copy(
            update={
                "id": uuid4(),
                "participants": [InteractionParticipant(person_id=pruned.id, role="sender")],
            }
        )
        link = PersonMemoryLink(id=uuid4(), person_id=pruned.id, belief_id=uuid4(), **common)
        relationship = RelationshipAssertion(
            id=uuid4(),
            subject=PeopleEndpoint(kind="person", id=pruned.id),
            object=PeopleEndpoint(kind="owner"),
            predicate="friend",
            belief_id=uuid4(),
            **common,
        )
        dependents: list[PeopleRecord] = [identifier, mention, shared, solo, link, relationship]
        for dependent in dependents:
            await store.put(dependent, expected_revision=0)
        # Detach what survives, the way the repair does before it erases.
        await store.put(
            identifier.model_copy(update={"person_id": None, "revision": 2, **later}),
            expected_revision=1,
        )
        await store.put(
            mention.model_copy(update={"person_id": None, "revision": 2, **later}),
            expected_revision=1,
        )
        await store.put(
            shared.model_copy(
                update={
                    "participants": [InteractionParticipant(person_id=kept.id, role="recipient")],
                    "revision": 2,
                    **later,
                }
            ),
            expected_revision=1,
        )
        return {
            "pruned": pruned,
            "kept": kept,
            "identifier": identifier,
            "mention": mention,
            "shared": shared,
            "solo": solo,
            "link": link,
            "relationship": relationship,
        }

    rows = await fixture()
    await store.erase(owner, [rows["pruned"].id], preserve_independent=True)
    for name in ("pruned", "link", "relationship", "solo"):
        assert await store.is_erased(owner, rows[name].id), name
    for name in ("identifier", "mention", "shared", "kept"):
        assert not await store.is_erased(owner, rows[name].id), name
        assert await store.get(owner, rows[name].id, ceiling=Sensitivity.RESTRICTED) is not None
    # The survivors' revisions that still named the erased person are gone.
    assert (
        await store.get(owner, rows["identifier"].id, ceiling=Sensitivity.RESTRICTED, at_revision=1)
        is None
    )
    detached = await store.get(owner, rows["identifier"].id, ceiling=Sensitivity.RESTRICTED)
    assert isinstance(detached, PersonIdentifier) and detached.person_id is None
    # A detached endpoint can be adopted by someone else later.
    await store.put(
        detached.model_copy(
            update={
                "person_id": rows["kept"].id,
                "revision": detached.revision + 1,
                "updated_at": NOW + timedelta(days=2),
            }
        ),
        expected_revision=detached.revision,
    )

    # Control: a plain erase follows historical revisions and takes the detached rows too.
    control = await fixture()
    await store.erase(owner, [control["pruned"].id])
    assert await store.is_erased(owner, control["identifier"].id)
    assert await store.is_erased(owner, control["mention"].id)


async def test_memory_people_preserving_erase_contract() -> None:
    await people_preserving_erase_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_identifier_lookup_contract(store: PeopleStore) -> None:
    """Exact, attached, distinct identifier lookup is immune to per-message copies (ADR-0121).

    Correspondence writes one identifier row per message. A substring search
    capped at one hundred rows made a frequent correspondent's address
    ambiguous; resolution instead asks for each distinct assignment once.
    """
    owner = principal()
    common = _admission_fields()
    address = f"frequent-{uuid4().hex[:8]}@example.test"
    source = PeopleSource(
        id=uuid4(),
        session_id=uuid4(),
        event_sequence=1,
        source_kind="owner",
        source_revision="event@1",
        evidence_at=NOW,
        **common,
    )
    frequent = Person(id=uuid4(), display_name="Frequent correspondent", **common)
    other = Person(id=uuid4(), display_name="Superstring owner", **common)
    for row in (source, frequent, other):
        await store.put(row, expected_revision=0)

    def identifier(person_id: object, value: str, **changes: object) -> PersonIdentifier:
        fields: dict[str, object] = {
            "id": uuid4(),
            "person_id": person_id,
            "identifier_kind": "email",
            "namespace": "owner",
            "value": value,
            "context": "owner",
            "verification": "channel_observed",
            "valid_from": NOW,
            "support_ids": [source.id],
            **common,
            **changes,
        }
        return PersonIdentifier.model_validate(fields)

    for _ in range(150):
        await store.put(identifier(frequent.id, address), expected_revision=0)
    unattached_ids: set[object] = set()
    for _ in range(150):
        endpoint = identifier(None, address)
        unattached_ids.add(endpoint.id)
        await store.put(endpoint, expected_revision=0)
    await store.put(identifier(other.id, "a" + address), expected_revision=0)
    # Valid only later, in its own context, so it stays a distinct assignment.
    await store.put(
        identifier(
            frequent.id, address, context="email:later", valid_from=NOW + timedelta(days=10)
        ),
        expected_revision=0,
    )
    base = PeopleQuery(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        kinds=["identifier"],
        identifier_value=address.upper(),
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=100,
    )
    attached = await store.query(
        base.model_copy(
            update={"assigned": "attached", "distinct_assignments": True, "valid_at": NOW}
        )
    )
    assert [
        (row.person_id, row.context) for row in attached if isinstance(row, PersonIdentifier)
    ] == [(frequent.id, "owner")]
    everything = await store.query(
        base.model_copy(update={"assigned": "attached", "distinct_assignments": True})
    )
    assert {row.context for row in everything if isinstance(row, PersonIdentifier)} == {
        "owner",
        "email:later",
    }
    seen: set[object] = set()
    after = None
    while True:
        page = await store.query(base.model_copy(update={"assigned": "unattached", "after": after}))
        assert all(isinstance(row, PersonIdentifier) and row.person_id is None for row in page)
        seen.update(row.id for row in page[:100])
        if len(page) <= 100:
            break
        after = page[99].id
    assert seen == unattached_ids
    # Ending an assignment pages through every open copy one person holds.
    copies: set[object] = set()
    after = None
    while True:
        page = await store.query(
            base.model_copy(
                update={
                    "person_id": frequent.id,
                    "assigned": "attached",
                    "valid_at": NOW,
                    "after": after,
                }
            )
        )
        assert all(isinstance(row, PersonIdentifier) for row in page)
        copies.update(row.id for row in page[:100])
        if len(page) <= 100:
            break
        after = page[99].id
    assert len(copies) == 150

    # Ending and later re-adding the same assignment must retain both periods.
    ended_at = NOW + timedelta(days=1)
    for _ in range(3):
        await store.put(identifier(frequent.id, address, valid_to=ended_at), expected_revision=0)
    periods = await store.query(
        base.model_copy(update={"person_id": frequent.id, "distinct_assignments": True})
    )
    assert {
        (row.context, row.valid_to) for row in periods if isinstance(row, PersonIdentifier)
    } == {("owner", None), ("owner", ended_at), ("email:later", None)}


async def test_memory_people_identifier_lookup_contract() -> None:
    await people_identifier_lookup_contract(InMemoryPeopleStore(FixedClock(NOW)))


async def people_review_directory_contract(store: PeopleStore) -> None:
    """Needs review holds unconfirmed people known only by name or role (ADR-0121)."""
    owner = principal()
    common = _admission_fields()
    marker = f"review-{uuid4().hex[:8]}"
    source = PeopleSource(
        id=uuid4(),
        session_id=uuid4(),
        event_sequence=1,
        source_kind="owner",
        source_revision="event@1",
        evidence_at=NOW,
        **common,
    )
    await store.put(source, expected_revision=0)

    async def person(label: str, **changes: object) -> Person:
        row = Person(id=uuid4(), display_name=f"{marker} {label}", **{**common, **changes})  # type: ignore[arg-type]
        await store.put(row, expected_revision=0)
        return row

    async def alias(person_id: UUID, kind: str, verification: str) -> PersonIdentifier:
        row = PersonIdentifier(
            id=uuid4(),
            person_id=person_id,
            identifier_kind=kind,  # type: ignore[arg-type]
            namespace="owner",
            value=f"{marker}-{uuid4().hex[:6]}@example.test" if kind == "email" else marker,
            context="owner",
            verification=verification,  # type: ignore[arg-type]
            valid_from=NOW,
            support_ids=[source.id],
            **common,
        )
        await store.put(row, expected_revision=0)
        return row

    named_only = await person("named only")
    await alias(named_only.id, "name", "contextual")
    bare = await person("bare")
    correspondent = await person("correspondent")
    await alias(correspondent.id, "email", "channel_observed")
    confirmed = await person("confirmed")
    await alias(confirmed.id, "name", "owner_confirmed")
    await person("active", state="active")
    await person("pinned", pinned=True)
    await person("merged", state="merged", merged_into=named_only.id)
    detached_owner = await person("detached")
    detached = await alias(detached_owner.id, "email", "channel_observed")
    await store.put(
        detached.model_copy(
            update={"person_id": None, "revision": 2, "updated_at": NOW + timedelta(days=1)}
        ),
        expected_revision=1,
    )
    query = PeopleQuery(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        text=marker,
        needs_review=True,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=1,
    )
    expected = sorted([named_only.id, bare.id, detached_owner.id])
    seen: list[UUID] = []
    after = None
    while True:
        page = await store.query(query.model_copy(update={"after": after}))
        seen.extend(row.id for row in page[:1])
        if len(page) <= 1:
            break
        after = page[0].id
    assert seen == expected


async def test_memory_people_review_directory_contract() -> None:
    await people_review_directory_contract(InMemoryPeopleStore(FixedClock(NOW)))
