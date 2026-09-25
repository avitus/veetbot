"""Duplicate people merge automatically only on decisive evidence (ADR-0125)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.people_duplicates import PeopleDeduplicator
from agent_core.application.people_identity import PeopleIdentityService
from agent_core.domain.email import EmailAccount, EmailRecord
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    InteractionParticipant,
    PeopleInteraction,
    PeopleMergeSuggestion,
    PeopleOperation,
    PeopleQuery,
    Person,
    PersonIdentifier,
)
from agent_core.domain.people_views import PeopleIdentityRequest, ResolveMergeSuggestion
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, memory_uow_factory, principal, session

OWNER = principal().model_copy(update={"scopes": {"people.read", "people.write"}})


def _fields(days: int = 10) -> PeopleFields:
    return {
        "tenant_id": OWNER.tenant_id,
        "principal_id": OWNER.principal_id,
        "created_at": NOW - timedelta(days=days),
        "updated_at": NOW - timedelta(days=days),
    }


async def _stack() -> tuple[Any, Any, PeopleIdentityService, PeopleDeduplicator]:
    clock, factory = await memory_uow_factory()
    async with factory() as uow:
        await uow.email.put(
            EmailRecord(
                kind="account",
                key="personal",
                revision=1,
                payload=EmailAccount(
                    id="personal",
                    label="Personal",
                    email_address="avitus@example.test",
                    status="syncing",
                ).model_dump(mode="json"),
                **_fields(),
            ),
            expected_revision=0,
        )
    ids = SequenceIdFactory(UUID(int=value) for value in range(700_000, 701_000))
    identity = PeopleIdentityService(factory, clock, ids)
    return clock, factory, identity, PeopleDeduplicator(factory, clock, identity)


async def _person(
    factory: Any,
    name: str,
    *,
    state: str = "provisional",
    observed: tuple[str, ...] = (),
    confirmed: tuple[str, ...] = (),
    history: int = 0,
    days: int = 10,
) -> Person:
    person = Person(id=uuid4(), display_name=name, state=state, **_fields(days))  # type: ignore[arg-type]
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        for address, verification in [(a, "channel_observed") for a in observed] + [
            (a, "owner_confirmed") for a in confirmed
        ]:
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="email",
                    namespace="owner",
                    value=address,
                    context="owner",
                    verification=verification,  # type: ignore[arg-type]
                    valid_from=NOW - timedelta(days=days),
                    **_fields(days),
                ),
                expected_revision=0,
            )
        for _ in range(history):
            await uow.people.put(
                PeopleInteraction(
                    id=uuid4(),
                    channel="email",
                    interaction_kind="exchange",
                    attribution="observed",
                    direction="outgoing",
                    summary="Sent email",
                    occurred_at=NOW - timedelta(days=1),
                    participants=[InteractionParticipant(person_id=person.id, role="recipient")],
                    **_fields(days),
                ),
                expected_revision=0,
            )
    return person


async def _rows(factory: Any, kind: str) -> list[Any]:
    async with factory() as uow:
        return list(
            await uow.people.query(
                PeopleQuery(
                    tenant_id=OWNER.tenant_id,
                    principal_id=OWNER.principal_id,
                    kinds=[kind],
                    sensitivity_ceiling=Sensitivity.RESTRICTED,
                    limit=100,
                )
            )
        )


async def _suggestions(factory: Any) -> list[PeopleMergeSuggestion]:
    return [row for row in await _rows(factory, "merge_suggestion") if row.state == "open"]


async def test_owner_assigned_address_merges_the_correspondent_automatically() -> None:
    """Giving Erin the address a correspondent was created from settles who that was."""
    _clock, factory, _identity, duplicates = await _stack()
    erin = await _person(factory, "Erin", state="active", confirmed=("erin@home.test",))
    written = await _person(factory, "Erin Vitus", observed=("erin@home.test",), history=3)
    report = await duplicates.run(OWNER, apply=True)
    assert [(m.source_id, m.target_id, m.reason) for m in report.merges] == [
        (written.id, erin.id, "same_address")
    ]
    async with factory() as uow:
        merged = await uow.people.get(OWNER, written.id, ceiling=Sensitivity.RESTRICTED)
    assert isinstance(merged, Person) and merged.merged_into == erin.id
    history = await _rows(factory, "interaction")
    assert all(p.person_id == erin.id for row in history for p in row.participants)
    [operation] = await _rows(factory, "operation")
    assert isinstance(operation, PeopleOperation)
    assert operation.automatic and operation.state == "completed"
    assert await _suggestions(factory) == []


async def test_an_address_the_owner_gave_two_people_is_shared_not_merged() -> None:
    _clock, factory, _identity, duplicates = await _stack()
    await _person(factory, "Maya", state="active", confirmed=("family@home.test",))
    await _person(factory, "Nora", state="active", confirmed=("family@home.test",))
    report = await duplicates.run(OWNER, apply=True)
    assert report.merges == [] and await _suggestions(factory) == []


async def test_name_matches_are_suggested_never_merged() -> None:
    """Two people can share a name (gate P01), so a name alone only asks the owner."""
    _clock, factory, _identity, duplicates = await _stack()
    erin = await _person(factory, "Erin", state="active")
    erin_written = await _person(factory, "Erin Vitus", observed=("erin@home.test",), history=2)
    sabina = await _person(factory, "Sabina Smith", observed=("sabina@home.test",), history=4)
    sabina_work = await _person(factory, "Sabina Smith", observed=("sabina@work.test",), history=1)
    kyrri = await _person(factory, "Kyrri", state="active")
    kyrriana = await _person(factory, "Kyrriana Vitus", observed=("kyrriana@home.test",))
    await _person(factory, "Dana Reyes", observed=("dana@work.test",))
    report = await duplicates.run(OWNER, apply=True)
    assert report.merges == []
    found = {
        (row.source_id, row.target_id, row.reason, row.family_name)
        for row in await _suggestions(factory)
    }
    assert found == {
        (erin_written.id, erin.id, "first_name", True),
        (sabina_work.id, sabina.id, "same_name", False),
        (kyrriana.id, kyrri.id, "nickname", True),
    }
    assert {person.display_name for person in await _rows(factory, "person")} >= {"Erin Vitus"}


async def test_an_ambiguous_first_name_asks_only_about_the_family_match() -> None:
    _clock, factory, _identity, duplicates = await _stack()
    await _person(factory, "Dana", state="active")
    await _person(factory, "Dana Reyes", observed=("dana@work.test",))
    await _person(factory, "Dana Cho", observed=("cho@work.test",))
    erin = await _person(factory, "Erin", state="active")
    family = await _person(factory, "Erin Vitus", observed=("erin@home.test",))
    await _person(factory, "Erin Walsh", observed=("walsh@work.test",))
    await duplicates.run(OWNER, apply=True)
    assert [(row.source_id, row.target_id) for row in await _suggestions(factory)] == [
        (family.id, erin.id)
    ]


async def test_preview_writes_nothing() -> None:
    _clock, factory, _identity, duplicates = await _stack()
    erin = await _person(factory, "Erin", state="active", confirmed=("erin@home.test",))
    written = await _person(factory, "Erin Vitus", observed=("erin@home.test",))
    await _person(factory, "Sabina Smith", observed=("sabina@home.test",))
    await _person(factory, "Sabina Smith", observed=("sabina@work.test",))
    async with factory() as uow:
        before = await uow.people.watermark(OWNER)
    report = await duplicates.run(OWNER, apply=False)
    assert not report.applied
    assert [(m.source_id, m.target_id) for m in report.merges] == [(written.id, erin.id)]
    assert [s.reason for s in report.suggestions] == ["same_name"]
    async with factory() as uow:
        assert await uow.people.watermark(OWNER) == before


async def test_a_separated_pair_and_an_undone_merge_are_never_proposed_again() -> None:
    _clock, factory, identity, duplicates = await _stack()
    await _person(factory, "Sabina Smith", observed=("sabina@home.test",))
    await _person(factory, "Sabina Smith", observed=("sabina@work.test",))
    erin = await _person(factory, "Erin", state="active", confirmed=("erin@home.test",))
    await _person(factory, "Erin Vitus", observed=("erin@home.test",))
    first = await duplicates.run(OWNER, apply=True)
    [suggestion] = await _suggestions(factory)
    await duplicates.resolve(
        OWNER,
        suggestion.id,
        ResolveMergeSuggestion(
            session_id=session().id, expected_revision=suggestion.revision, decision="separate"
        ),
        key="separate-sabina",
        ceiling=Sensitivity.SENSITIVE,
    )
    [merge] = first.merges
    [operation] = await _rows(factory, "operation")
    undo = await identity.request(
        OWNER,
        PeopleIdentityRequest(
            session_id=session().id,
            operation="undo",
            operation_id=operation.id,
            expected_revision=operation.revision,
        ),
        key="undo-automatic",
        ceiling=Sensitivity.SENSITIVE,
    )
    await identity.request(
        OWNER,
        PeopleIdentityRequest(
            session_id=session().id,
            operation="apply",
            operation_id=undo.id,
            expected_revision=undo.revision,
        ),
        key="apply-undo",
        ceiling=Sensitivity.SENSITIVE,
    )
    again = await duplicates.run(OWNER, apply=True)
    assert again.merges == [] and again.suggestions == []
    async with factory() as uow:
        restored = await uow.people.get(OWNER, merge.source_id, ceiling=Sensitivity.RESTRICTED)
    assert isinstance(restored, Person) and restored.merged_into is None
    assert erin.id != merge.source_id


async def test_the_owner_confirms_a_suggestion_and_the_pair_merges() -> None:
    _clock, factory, _identity, duplicates = await _stack()
    sabina = await _person(factory, "Sabina Smith", observed=("sabina@home.test",), history=2)
    work = await _person(factory, "Sabina Smith", observed=("sabina@work.test",))
    await duplicates.run(OWNER, apply=True)
    [suggestion] = await _suggestions(factory)
    request = ResolveMergeSuggestion(
        session_id=session().id, expected_revision=suggestion.revision, decision="merge"
    )
    resolved = await duplicates.resolve(
        OWNER, suggestion.id, request, key="merge-sabina", ceiling=Sensitivity.SENSITIVE
    )
    assert resolved.state == "merged" and resolved.operation_id is not None
    replay = await duplicates.resolve(
        OWNER, suggestion.id, request, key="merge-sabina", ceiling=Sensitivity.SENSITIVE
    )
    assert replay == resolved
    async with factory() as uow:
        merged = await uow.people.get(OWNER, work.id, ceiling=Sensitivity.RESTRICTED)
        operation = await uow.people.get(
            OWNER, resolved.operation_id, ceiling=Sensitivity.RESTRICTED
        )
    assert isinstance(merged, Person) and merged.merged_into == sabina.id
    assert isinstance(operation, PeopleOperation) and not operation.automatic
    with pytest.raises(ConflictError):
        await duplicates.resolve(
            OWNER,
            suggestion.id,
            request.model_copy(update={"decision": "separate"}),
            key="late-separate",
            ceiling=Sensitivity.SENSITIVE,
        )


async def test_a_suggestion_withdraws_when_the_names_part_and_reopens_when_they_meet() -> None:
    _clock, factory, _identity, duplicates = await _stack()
    await _person(factory, "Sabina Smith", observed=("sabina@home.test",))
    work = await _person(factory, "Sabina Smith", observed=("sabina@work.test",))
    await duplicates.run(OWNER, apply=True)
    [opened] = await _suggestions(factory)

    async def rename(name: str) -> None:
        async with factory() as uow:
            current = await uow.people.get(OWNER, work.id, ceiling=Sensitivity.RESTRICTED)
            assert isinstance(current, Person)
            await uow.people.put(
                current.model_copy(
                    update={
                        "display_name": name,
                        "revision": current.revision + 1,
                        "updated_at": NOW + timedelta(seconds=current.revision),
                    }
                ),
                expected_revision=current.revision,
            )

    await rename("Sabina Ortiz")
    await duplicates.run(OWNER, apply=True)
    assert await _suggestions(factory) == []
    [withdrawn] = await _rows(factory, "merge_suggestion")
    assert withdrawn.id == opened.id and withdrawn.state == "withdrawn"
    await rename("Sabina Smith")
    await duplicates.run(OWNER, apply=True)
    [reopened] = await _suggestions(factory)
    assert reopened.id == opened.id and reopened.revision == 3
