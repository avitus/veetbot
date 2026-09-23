"""Conservative identity resolution must preserve ambiguous people."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.people import InMemoryPeopleStore
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import Person, PersonIdentifier
from agent_core.memory.people import resolve_identity
from tests.contract.support import NOW, principal


async def test_shared_identifiers_and_names_never_merge_people() -> None:
    store = InMemoryPeopleStore(FixedClock(NOW))
    owner = principal()
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    first = Person(id=uuid4(), display_name="Alex", **common)
    second = Person(id=uuid4(), display_name="Alex", **common)
    for person in (first, second):
        await store.put(person, expected_revision=0)
        await store.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=person.id,
                identifier_kind="email",
                namespace="work",
                value="team@example.test",
                verification="owner_confirmed",
                valid_from=NOW,
                **common,
            ),
            expected_revision=0,
        )
    result = await resolve_identity(
        store,
        owner,
        kind="email",
        namespace="work",
        value="team@example.test",
        context="",
        at=NOW,
        ceiling=Sensitivity.SENSITIVE,
    )
    assert result.status == "ambiguous"
    assert set(result.person_ids) == {first.id, second.id}
    unchanged = await store.get(owner, first.id, ceiling=Sensitivity.SENSITIVE)
    assert isinstance(unchanged, Person) and unchanged.revision == 1
    result = await resolve_identity(
        store,
        owner,
        kind="name",
        namespace="owner",
        value="Alex",
        context="",
        at=NOW,
        ceiling=Sensitivity.SENSITIVE,
    )
    assert result.status == "ambiguous"
    result = await resolve_identity(
        store,
        owner,
        kind="email",
        namespace="work",
        value="team@example.test",
        context="",
        at=NOW,
        ceiling=Sensitivity.INTERNAL,
    )
    assert result.status == "unresolved" and result.person_ids == []


async def test_identifier_assignment_respects_time_namespace_and_context() -> None:
    store = InMemoryPeopleStore(FixedClock(NOW))
    owner = principal()
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Alex", **common)
    await store.put(person, expected_revision=0)
    await store.put(
        PersonIdentifier(
            id=uuid4(),
            person_id=person.id,
            identifier_kind="email",
            namespace="work",
            value="Alex+team@EXAMPLE.test",
            context="company-a",
            verification="owner_confirmed",
            valid_from=NOW,
            valid_to=NOW + timedelta(days=10),
            **common,
        ),
        expected_revision=0,
    )
    arguments: dict[str, Any] = {
        "kind": "email",
        "namespace": "work",
        "value": "Alex+team@example.test",
        "context": "company-a",
        "at": NOW,
        "ceiling": Sensitivity.SENSITIVE,
    }
    assert (await resolve_identity(store, owner, **arguments)).status == "matched"
    for change in (
        {"at": NOW + timedelta(days=10)},
        {"namespace": "personal"},
        {"context": "company-b"},
        {"value": "alex@example.test"},
    ):
        assert (await resolve_identity(store, owner, **(arguments | change))).status == "unresolved"


async def test_contextual_roles_never_establish_unique_identity() -> None:
    store = InMemoryPeopleStore(FixedClock(NOW))
    owner = principal()
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="my daughter", **common)
    await store.put(person, expected_revision=0)
    await store.put(
        PersonIdentifier(
            id=uuid4(),
            person_id=person.id,
            identifier_kind="role",
            namespace="owner",
            value="my daughter",
            context="owner",
            verification="contextual",
            valid_from=NOW,
            **common,
        ),
        expected_revision=0,
    )
    result = await resolve_identity(
        store,
        owner,
        kind="role",
        namespace="owner",
        value="my daughter",
        context="owner",
        at=NOW,
        ceiling=Sensitivity.SENSITIVE,
    )
    assert result.status == "ambiguous"


async def test_supported_context_disambiguates_same_name_without_merging() -> None:
    store = InMemoryPeopleStore(FixedClock(NOW))
    owner = principal()
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    people = [Person(id=uuid4(), display_name="Alex", **common) for _ in range(2)]
    for person, context in zip(people, ("Acme", "choir"), strict=True):
        await store.put(person, expected_revision=0)
        await store.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=person.id,
                identifier_kind="name",
                namespace="owner",
                value="Alex",
                context=context,
                verification="contextual",
                valid_from=NOW,
                **common,
            ),
            expected_revision=0,
        )
    result = await resolve_identity(
        store,
        owner,
        kind="name",
        namespace="owner",
        value="Alex",
        context="choir",
        at=NOW,
        ceiling=Sensitivity.SENSITIVE,
    )
    assert result.status == "matched" and result.person_ids == [people[1].id]
    result = await resolve_identity(
        store,
        owner,
        kind="name",
        namespace="owner",
        value="Alex",
        context="owner",
        at=NOW,
        ceiling=Sensitivity.SENSITIVE,
    )
    assert result.status == "ambiguous"


def _fields() -> dict[str, Any]:
    owner = principal()
    return {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }


async def test_many_observed_copies_of_one_address_still_match() -> None:
    """Per-message identifier copies never turn one correspondent ambiguous (ADR-0118)."""
    store = InMemoryPeopleStore(FixedClock(NOW))
    owner = principal()
    person = Person(id=uuid4(), display_name="Frequent Correspondent", **_fields())
    await store.put(person, expected_revision=0)
    for person_id in [person.id] * 150 + [None] * 150:
        await store.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=person_id,
                identifier_kind="email",
                namespace="owner",
                value="frequent@example.test",
                context="owner",
                verification="channel_observed",
                valid_from=NOW,
                **_fields(),
            ),
            expected_revision=0,
        )
    result = await resolve_identity(
        store,
        owner,
        kind="email",
        namespace="owner",
        value="frequent@example.test",
        context="owner",
        at=NOW,
        ceiling=Sensitivity.RESTRICTED,
    )
    assert (result.status, result.person_ids) == ("matched", [person.id])


async def test_superstring_names_and_addresses_never_crowd_out_a_match() -> None:
    """Only exact values compete; 'Cheryl Smith' rows cannot hide 'Cheryl'."""
    store = InMemoryPeopleStore(FixedClock(NOW))
    owner = principal()
    cheryl = Person(id=uuid4(), display_name="Cheryl", **_fields())
    await store.put(cheryl, expected_revision=0)
    await store.put(
        PersonIdentifier(
            id=uuid4(),
            person_id=cheryl.id,
            identifier_kind="name",
            namespace="owner",
            value="Cheryl",
            context="owner",
            verification="owner_confirmed",
            valid_from=NOW,
            **_fields(),
        ),
        expected_revision=0,
    )
    for index in range(101):
        await store.put(
            Person(id=uuid4(), display_name=f"Cheryl Smith {index}", **_fields()),
            expected_revision=0,
        )
    result = await resolve_identity(
        store,
        owner,
        kind="name",
        namespace="owner",
        value="Cheryl",
        context="owner",
        at=NOW,
        ceiling=Sensitivity.RESTRICTED,
    )
    assert (result.status, result.person_ids) == ("matched", [cheryl.id])


async def test_owner_stated_address_matches_mail() -> None:
    """An address the owner stated in chat identifies the person for mail (ADR-0118)."""
    store = InMemoryPeopleStore(FixedClock(NOW))
    owner = principal()
    brother = Person(id=uuid4(), display_name="My brother", **_fields())
    await store.put(brother, expected_revision=0)
    await store.put(
        PersonIdentifier(
            id=uuid4(),
            person_id=brother.id,
            identifier_kind="email",
            namespace="owner",
            value="bob@example.test",
            context="owner",
            verification="contextual",
            valid_from=NOW,
            **_fields(),
        ),
        expected_revision=0,
    )
    result = await resolve_identity(
        store,
        owner,
        kind="email",
        namespace="owner",
        value="bob@example.test",
        context="owner",
        at=NOW,
        ceiling=Sensitivity.RESTRICTED,
    )
    assert (result.status, result.person_ids) == ("matched", [brother.id])
