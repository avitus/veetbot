"""A sender's own signature updates their contact details in People (ADR-0336)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import permutations
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.people import PublicPeopleService
from agent_core.application.people_duplicates import PeopleDeduplicator
from agent_core.application.people_identity import PeopleIdentityService
from agent_core.domain.email_signature import SIGNATURE_CONTEXT, SIGNATURE_NAMESPACE
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    PeopleMergeSuggestion,
    PeopleOperation,
    Person,
    PersonIdentifier,
)
from agent_core.domain.people_extraction import PersonEvidence
from agent_core.domain.people_views import AddPersonAlias, EndPersonAlias, UpdatePerson
from agent_core.memory.people import resolve_identity
from tests.contract.people_fixtures import PeopleFields
from tests.contract.people_mail_fixtures import (
    ALEX,
    OWNER,
    all_rows,
    correspondence_stack,
    mail,
    seed_account,
)
from tests.contract.support import NOW, principal, session

RORY = "Rory O'Driscoll <rory@scalevp.test>"
OFFICE = "650.555.0100"


def _signed(name: str = "Rory O'Driscoll", title: str = "Partner, Scale", phone: str = "") -> str:
    lines = ["Thanks, see you Thursday.", "", "-- ", name, title, phone or "650.378.6038"]
    return "\n".join(line for line in lines if line is not None) + "\n"


async def _stack(*known: str) -> tuple[Any, Any]:
    """A mailbox whose owner has written to each known correspondent."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    for index, contact in enumerate(known or (RORY,)):
        await service.register_source(
            await mail(
                factory,
                message_id=f"m-sent-{index}",
                sender=OWNER,
                to=contact,
                labels=["SENT"],
                days_ago=20,
            )
        )
    return factory, service


async def _receive(factory: Any, service: Any, message_id: str, **fields: Any) -> Any:
    fields.setdefault("sender", RORY)
    fields.setdefault("sender_check", "pass")
    fields.setdefault("body", _signed())
    source = await mail(factory, message_id=message_id, **fields)
    await service.register_source(source)
    return source


async def _people(factory: Any) -> dict[str, Person]:
    async with factory() as uow:
        return {
            row.display_name: row
            for row in await uow.people.query(all_rows(["person"]))
            if isinstance(row, Person)
        }


async def _details(factory: Any, person: Person | None = None) -> list[PersonIdentifier]:
    async with factory() as uow:
        rows = await uow.people.query(all_rows(["identifier"]))
    return sorted(
        (
            row
            for row in rows
            if isinstance(row, PersonIdentifier)
            and row.namespace == SIGNATURE_NAMESPACE
            and (person is None or row.person_id == person.id)
        ),
        key=lambda row: (row.valid_from, row.identifier_kind, row.value),
    )


def _current(rows: list[PersonIdentifier]) -> set[tuple[str, str]]:
    return {(row.identifier_kind, row.value) for row in rows if row.valid_to is None}


async def test_a_known_senders_signature_adds_their_number_and_title() -> None:
    factory, service = await _stack()
    source = await _receive(factory, service, "m-in", days_ago=3)
    rory = (await _people(factory))["Rory O'Driscoll"]

    rows = await _details(factory)

    assert _current(rows) == {("phone", "650.378.6038"), ("role", "Partner, Scale")}
    for row in rows:
        # The sender's own claim: the weakest verification, in a reserved namespace.
        assert row.person_id == rory.id
        assert row.verification == "contextual" and row.context == SIGNATURE_CONTEXT
        assert row.valid_from == source.sent_at and len(row.support_ids) == 1
        assert row.value in source.body
    # A second read of the same message writes nothing more.
    await service.register_source(source)
    assert await _details(factory) == rows
    # The profile lists both, each once, beside the address the mail came from.
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    profile = await PublicPeopleService(factory, service._clock).get(
        owner, rory.id, ceiling=Sensitivity.SENSITIVE
    )
    listed = [(alias.identifier_kind, alias.value) for alias in profile.aliases]
    assert sorted(pair for pair in listed if pair[0] in {"phone", "role"}) == [
        ("phone", "650.378.6038"),
        ("role", "Partner, Scale"),
    ]
    assert ("email", "rory@scalevp.test") in listed


@pytest.mark.parametrize("sender_check", ["fail", "none", None])
async def test_mail_gmail_did_not_authenticate_adds_nothing(sender_check: str | None) -> None:
    factory, service = await _stack()
    await _receive(factory, service, "m-in", sender_check=sender_check)
    assert await _details(factory) == []


async def test_a_page_without_the_whole_body_adds_nothing() -> None:
    """The signature ends the text, so a truncated page cannot show whose it is."""
    factory, service = await _stack()
    await _receive(factory, service, "m-in", body_complete=False)
    assert await _details(factory) == []


async def test_a_borrowed_display_name_updates_nobody() -> None:
    """The person is chosen by the authenticated address, never by the name on the mail."""
    factory, service = await _stack()
    await _receive(factory, service, "m-in", sender="Rory O'Driscoll <rory@lookalike.test>")
    assert await _details(factory) == []


async def test_a_sender_not_yet_in_people_adds_nothing() -> None:
    factory, service = await _stack()
    await _receive(
        factory,
        service,
        "m-in",
        sender="Dana Stranger <dana@else.test>",
        body=_signed("Dana Stranger"),
    )
    assert await _details(factory) == []


async def test_the_owners_own_signature_adds_nothing() -> None:
    factory, service = await _stack()
    await service.register_source(
        await mail(
            factory,
            message_id="m-out",
            sender=OWNER,
            to=RORY,
            labels=["SENT"],
            body=_signed("Owner Name", "Founder"),
            sender_check="pass",
        )
    )
    assert await _details(factory) == []


async def test_a_signature_in_quoted_history_adds_nothing() -> None:
    """Alex's reply quotes Rory's signature; neither of them is updated by it."""
    factory, service = await _stack(RORY, ALEX)
    quoted = "\n".join("> " + line for line in _signed().splitlines())
    body = "Agreed.\n\nOn Tue, Nov 17, 2026 at 9:00 AM Rory O'Driscoll wrote:\n" + quoted + "\n"
    await _receive(factory, service, "m-alex", sender=ALEX, body=body)
    # Rory's own later reply carries only the old signature inside its history.
    await _receive(factory, service, "m-rory", body=body, days_ago=2)
    assert await _details(factory) == []


async def test_a_forwarded_signature_adds_nothing() -> None:
    factory, service = await _stack(RORY, ALEX)
    forwarded = (
        "FYI\n\n---------- Forwarded message ---------\n"
        "From: Rory O'Driscoll <rory@scalevp.test>\nSubject: Deck\n\n" + _signed()
    )
    await _receive(factory, service, "m-alex", sender=ALEX, body=forwarded)
    await _receive(factory, service, "m-rory", body=forwarded, days_ago=2)
    assert await _details(factory) == []


async def test_a_shared_number_neither_merges_nor_identifies_anyone() -> None:
    """Two people sign with one office line, and the owner gave that line to a third."""
    factory, service = await _stack(RORY, ALEX)
    await _receive(factory, service, "m-rory", body=_signed(phone="+1 650 555 0100"))
    await _receive(
        factory,
        service,
        "m-alex",
        sender=ALEX,
        body=_signed("Alex Rivera", "Analyst", "+1 (650) 555-0100"),
    )
    people = await _people(factory)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    front_desk = Person(id=uuid4(), display_name="Maya", state="active", **common)
    async with factory() as uow:
        await uow.people.put(front_desk, expected_revision=0)
        await uow.people.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=front_desk.id,
                identifier_kind="phone",
                namespace="owner",
                value="+16505550100",
                context="owner",
                verification="owner_confirmed",
                valid_from=NOW - timedelta(days=30),
                **common,
            ),
            expected_revision=0,
        )
    held = await _details(factory)
    assert {row.person_id for row in held if row.identifier_kind == "phone"} == {
        people["Rory O'Driscoll"].id,
        people["Alex Rivera"].id,
    }

    clock = service._clock
    ids = SequenceIdFactory(UUID(int=value) for value in range(800_000, 801_000))
    identity = PeopleIdentityService(factory, clock, ids)
    report = await PeopleDeduplicator(factory, clock, identity).run(owner, apply=True)

    assert report.merges == []
    async with factory() as uow:
        rows = await uow.people.query(all_rows(["person", "merge_suggestion", "operation"]))
        assert not [
            row for row in rows if isinstance(row, (PeopleMergeSuggestion, PeopleOperation))
        ]
        assert all(row.merged_into is None for row in rows if isinstance(row, Person))
        # The number still identifies only the person the owner gave it to.
        given = await resolve_identity(
            uow.people,
            owner,
            kind="phone",
            namespace="owner",
            value="+16505550100",
            context="owner",
            at=clock.now(),
            ceiling=Sensitivity.RESTRICTED,
        )
        assert given.status == "matched" and given.person_ids == [front_desk.id]
        for value in ("+1 650 555 0100", "+16505550100"):
            signed = await resolve_identity(
                uow.people,
                owner,
                kind="phone",
                namespace=SIGNATURE_NAMESPACE,
                value=value,
                context=SIGNATURE_CONTEXT,
                at=clock.now(),
                ceiling=Sensitivity.RESTRICTED,
            )
            assert signed.status == "unresolved" and signed.person_ids == []


@pytest.mark.parametrize("order", [("m-old", "m-new"), ("m-new", "m-old")])
async def test_a_changed_title_ends_the_earlier_one_in_any_processing_order(
    order: tuple[str, str],
) -> None:
    factory, service = await _stack()
    mails = {
        "m-old": {"days_ago": 6, "body": _signed(title="Principal, Scale")},
        "m-new": {"days_ago": 2, "body": _signed(title="Partner, Scale")},
    }
    sent: dict[str, datetime] = {}
    for message_id in order:
        sent[message_id] = (
            await _receive(factory, service, message_id, **mails[message_id])
        ).sent_at

    rows = await _details(factory)

    titles = {row.value: row for row in rows if row.identifier_kind == "role"}
    assert titles["Principal, Scale"].valid_from == sent["m-old"]
    assert titles["Principal, Scale"].valid_to == sent["m-new"]
    assert titles["Partner, Scale"].valid_from == sent["m-new"]
    assert titles["Partner, Scale"].valid_to is None
    # The number both signatures state stays current.
    assert _current(rows) == {("phone", "650.378.6038"), ("role", "Partner, Scale")}


async def test_an_older_signature_arriving_late_takes_its_place_in_the_sequence() -> None:
    """Analyst, then Director, then the Partner signature that was sent between them."""
    factory, service = await _stack()
    sent: dict[str, datetime] = {}
    for message_id, days_ago, title in (
        ("m-1", 6, "Analyst, Scale"),
        ("m-3", 2, "Director, Scale"),
        ("m-2", 4, "Partner, Scale"),
    ):
        sent[message_id] = (
            await _receive(
                factory, service, message_id, days_ago=days_ago, body=_signed(title=title)
            )
        ).sent_at

    rows = await _details(factory)

    titles = {
        row.value: (row.valid_from, row.valid_to) for row in rows if row.identifier_kind == "role"
    }
    # One title at a time: Analyst ends where Partner begins, not where Director does.
    assert titles == {
        "Analyst, Scale": (sent["m-1"], sent["m-2"]),
        "Partner, Scale": (sent["m-2"], sent["m-3"]),
        "Director, Scale": (sent["m-3"], None),
    }


# Each history is its signatures oldest first, as (days ago, body).
_HISTORIES: dict[str, list[tuple[int, str]]] = {
    "three titles": [
        (6, _signed(title="Analyst, Scale")),
        (4, _signed(title="Partner, Scale")),
        (2, _signed(title="Director, Scale")),
    ],
    "a title that returns": [
        (8, _signed(title="Analyst, Scale")),
        (6, _signed(title="Partner, Scale")),
        (4, _signed(title="Analyst, Scale")),
        (2, _signed(title="Partner, Scale")),
    ],
    "numbers that overlap": [
        (6, _signed(phone="650.555.0100")),
        (4, _signed(phone="650.555.0100 | 650.555.0142")),
        (2, _signed(phone="650.555.0142")),
    ],
}


async def _erase(factory: Any, service: Any, message_id: str, entry: str = "mailbox") -> None:
    """Remove one message the way the product does: excluded, or erased with its mailbox data."""
    if entry == "excluded":
        await service.exclude_source("work", "t-mail", message_id)
        return
    async with factory() as uow:
        await uow.session_deletions.erase_email_source(
            principal(), "work", "t-mail", frozenset({message_id}), service._clock.now()
        )


async def _intervals(
    history: list[tuple[int, str]], order: Iterable[int], erase: tuple[int, int] | None = None
) -> dict[tuple[datetime, str, str], datetime | None]:
    """The recorded intervals; `erase` is a message and how many are read before it goes."""
    factory, service = await _stack()
    for count, index in enumerate(order, start=1):
        days_ago, body = history[index]
        await _receive(factory, service, f"m-{index}", days_ago=days_ago, body=body)
        if erase is not None and erase[1] == count:
            await _erase(factory, service, f"m-{erase[0]}")
    return {
        (row.valid_from, row.identifier_kind, row.value): row.valid_to
        for row in await _details(factory)
    }


@pytest.mark.parametrize("name", list(_HISTORIES))
async def test_signatures_hold_the_same_intervals_in_any_processing_order(name: str) -> None:
    history = _HISTORIES[name]
    chronological = await _intervals(history, range(len(history)))
    for order in permutations(range(len(history))):
        assert await _intervals(history, order) == chronological, order


@pytest.mark.parametrize("name", list(_HISTORIES))
async def test_an_erased_signature_leaves_what_the_remaining_mail_alone_gives(name: str) -> None:
    """Read in any order and erased at any point after it was read."""
    history = _HISTORIES[name]
    for gone in range(len(history)):
        never_read = await _intervals(history, [i for i in range(len(history)) if i != gone])
        for order in permutations(range(len(history))):
            for read in range(order.index(gone) + 1, len(history) + 1):
                assert await _intervals(history, order, (gone, read)) == never_read, (
                    gone,
                    order,
                    read,
                )


@pytest.mark.parametrize("entry", ["excluded", "mailbox"])
async def test_erasing_the_newest_signature_makes_the_earlier_title_current_again(
    entry: str,
) -> None:
    """Analyst, then Director: with the Director message gone, Analyst is the title again."""
    factory, service = await _stack()
    old = await _receive(
        factory, service, "m-old", days_ago=6, body=_signed(title="Analyst, Scale")
    )
    await _receive(factory, service, "m-new", days_ago=2, body=_signed(title="Director, Scale"))

    await _erase(factory, service, "m-new", entry)

    assert {
        (row.identifier_kind, row.value): (row.valid_from, row.valid_to)
        for row in await _details(factory)
    } == {
        ("phone", "650.378.6038"): (old.sent_at, None),
        ("role", "Analyst, Scale"): (old.sent_at, None),
    }
    rory = (await _people(factory))["Rory O'Driscoll"]
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    profile = await PublicPeopleService(factory, service._clock).get(
        owner, rory.id, ceiling=Sensitivity.SENSITIVE
    )
    assert [
        (alias.value, alias.valid_to)
        for alias in profile.aliases
        if alias.identifier_kind == "role"
    ] == [("Analyst, Scale", None)]


async def test_erasing_a_signature_leaves_the_owners_ending_alone() -> None:
    """The owner ended a number at the very moment a later message was sent."""
    factory, service = await _stack()
    await _receive(factory, service, "m-first", days_ago=5, body=_signed(phone="650.555.0100"))
    rory = (await _people(factory))["Rory O'Driscoll"]
    [number] = [row for row in await _details(factory) if row.identifier_kind == "phone"]
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await PublicPeopleService(factory, service._clock).update(
        owner,
        rory.id,
        UpdatePerson(
            session_id=session().id,
            expected_revision=rory.revision,
            alias=EndPersonAlias(
                operation="end",
                identifier_id=number.id,
                expected_revision=number.revision,
                valid_to=(NOW - timedelta(days=2)).replace(microsecond=0),
            ),
        ),
        key="end-signature-number",
        ceiling=Sensitivity.SENSITIVE,
    )
    [ended] = [row for row in await _details(factory) if row.identifier_kind == "phone"]
    later = await _receive(
        factory, service, "m-later", days_ago=2, body=_signed(phone="650.555.0142")
    )
    assert ended.valid_to == later.sent_at

    await _erase(factory, service, "m-later")

    assert [row for row in await _details(factory) if row.identifier_kind == "phone"] == [ended]


async def test_a_late_older_signature_leaves_the_owners_ending_alone() -> None:
    """The owner said when a number ended; a signature sent before then does not move it."""
    factory, service = await _stack()
    await _receive(factory, service, "m-first", days_ago=5, body=_signed(phone="650.555.0100"))
    rory = (await _people(factory))["Rory O'Driscoll"]
    [number] = [row for row in await _details(factory) if row.identifier_kind == "phone"]
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await PublicPeopleService(factory, service._clock).update(
        owner,
        rory.id,
        UpdatePerson(
            session_id=session().id,
            expected_revision=rory.revision,
            alias=EndPersonAlias(
                operation="end",
                identifier_id=number.id,
                expected_revision=number.revision,
                valid_to=NOW - timedelta(days=1),
            ),
        ),
        key="end-signature-number",
        ceiling=Sensitivity.SENSITIVE,
    )
    [ended] = [row for row in await _details(factory) if row.identifier_kind == "phone"]

    await _receive(factory, service, "m-late", days_ago=3, body=_signed(phone="650.555.0142"))

    numbers = {row.value: row for row in await _details(factory) if row.identifier_kind == "phone"}
    assert numbers["650.555.0100"] == ended
    assert numbers["650.555.0142"].valid_to is None


async def test_a_sender_with_more_aliases_than_one_page_is_still_placed() -> None:
    """What the signatures have stated is read a page at a time, each alias once."""
    factory, service = await _stack()
    rory = (await _people(factory))["Rory O'Driscoll"]
    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    await _receive(factory, service, "m-old", days_ago=6, body=_signed(title="Principal, Scale"))
    async with factory() as uow:
        for number in range(120):
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=rory.id,
                    identifier_kind="name",
                    namespace="owner",
                    value=f"Rory Alias {number}",
                    context="owner",
                    verification="owner_confirmed",
                    valid_from=NOW - timedelta(days=30),
                    **common,
                ),
                expected_revision=0,
            )
    newer = await _receive(
        factory, service, "m-new", days_ago=2, body=_signed(title="Partner, Scale")
    )

    async with factory() as uow:
        titles = {
            row.value: row.valid_to
            for title in ("principal, scale", "partner, scale")
            for row in await uow.people.query(
                all_rows(["identifier"]).model_copy(
                    update={"person_id": rory.id, "identifier_value": title}
                )
            )
            if isinstance(row, PersonIdentifier) and row.namespace == SIGNATURE_NAMESPACE
        }
    assert titles == {"Principal, Scale": newer.sent_at, "Partner, Scale": None}


async def test_a_later_signature_replaces_the_numbers_it_states() -> None:
    factory, service = await _stack()
    await _receive(factory, service, "m-old", days_ago=6, body=_signed(phone="650.555.0100"))
    newer = await _receive(
        factory, service, "m-new", days_ago=2, body=_signed(phone="650.555.0142")
    )
    # A later note signed with the name and title only says nothing about numbers.
    await _receive(
        factory,
        service,
        "m-short",
        days_ago=1,
        body="Thanks.\n\nRory O'Driscoll\nPartner, Scale\n",
    )

    rows = await _details(factory)

    assert _current(rows) == {("phone", "650.555.0142"), ("role", "Partner, Scale")}
    [old] = [row for row in rows if row.value == "650.555.0100"]
    assert old.valid_to == newer.sent_at


async def test_erasing_the_source_email_removes_its_details() -> None:
    factory, service = await _stack()
    await _receive(factory, service, "m-first", days_ago=5)
    await _receive(factory, service, "m-second", days_ago=2, thread_id="t-other")
    assert len(await _details(factory)) == 4

    async with factory() as uow:
        await uow.people.erase_email_source(principal(), "work", "t-mail", frozenset({"m-first"}))
    remaining = await _details(factory)
    # The other message still states both details, so they remain on its support.
    assert _current(remaining) == {("phone", "650.378.6038"), ("role", "Partner, Scale")}
    assert len(remaining) == 2

    async with factory() as uow:
        await uow.people.erase_email_source(principal(), "work", "t-other", frozenset())
    assert await _details(factory) == []
    # An erased source never writes its details again.
    rory = (await _people(factory))["Rory O'Driscoll"]
    assert await _details(factory, rory) == []


async def test_a_detail_the_owner_ended_is_not_added_again() -> None:
    factory, service = await _stack()
    await _receive(factory, service, "m-first", days_ago=5)
    rory = (await _people(factory))["Rory O'Driscoll"]
    [number] = [row for row in await _details(factory) if row.identifier_kind == "phone"]
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    people = PublicPeopleService(factory, service._clock)
    await people.update(
        owner,
        rory.id,
        UpdatePerson(
            session_id=session().id,
            expected_revision=rory.revision,
            alias=EndPersonAlias(
                operation="end",
                identifier_id=number.id,
                expected_revision=number.revision,
                valid_to=NOW - timedelta(days=4),
            ),
        ),
        key="end-signature-number",
        ceiling=Sensitivity.SENSITIVE,
    )

    await _receive(factory, service, "m-second", days_ago=2)

    assert _current(await _details(factory)) == {("role", "Partner, Scale")}


@pytest.mark.parametrize("number", [OFFICE, "+16505550100"])
async def test_a_signature_number_never_selects_chat_context(number: str) -> None:
    from agent_core.application.people_context import PeopleContextService
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from tests.contract.memory_fixtures import recall_query
    from tests.contract.support import SESSION_ID, ids

    factory, service = await _stack()
    await _receive(factory, service, "m-in", body=_signed(phone=number))
    rory = (await _people(factory))["Rory O'Driscoll"]
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    context = PeopleContextService(
        factory, HybridMemoryRetriever(factory, service._clock, ids(), owner)
    )

    by_number = await context.automatic_recall(
        owner, recall_query(text=f"Who do I reach on {number} today?"), session_id=SESSION_ID
    )
    by_name = await context.automatic_recall(
        owner, recall_query(text="Prepare for my call with Rory O'Driscoll"), session_id=SESSION_ID
    )

    assert all(rory.id not in item.person_ids for item in by_number.people)
    assert any(rory.id in item.person_ids for item in by_name.people)


async def test_the_signature_namespace_is_reserved() -> None:
    """Nothing but the signature projection can write a row that reads as one."""
    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    for fields in (
        {"verification": "owner_confirmed"},
        {"verification": "channel_observed"},
        {"context": "owner"},
        {"identifier_kind": "name", "value": "Rory O'Driscoll"},
        {"identifier_kind": "email", "value": "rory@scalevp.test"},
    ):
        row = {
            "id": uuid4(),
            "person_id": uuid4(),
            "identifier_kind": "phone",
            "namespace": SIGNATURE_NAMESPACE,
            "value": OFFICE,
            "context": SIGNATURE_CONTEXT,
            "verification": "contextual",
            "valid_from": NOW,
            **common,
            **fields,
        }
        with pytest.raises(ValidationError, match="signature"):
            PersonIdentifier(**row)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="signature"):
        # A model-proposed mention cannot name it either.
        PersonEvidence(
            key="rory",
            source_event_id=1,
            start=0,
            end=len(OFFICE),
            text=OFFICE,
            display_name="Rory",
            identifier_kind="phone",
            identifier_value=OFFICE,
            namespace=SIGNATURE_NAMESPACE,
            context=SIGNATURE_CONTEXT,
            role="mentioned",
            referent_key=None,
        )
    with pytest.raises(ValidationError, match="signature"):
        AddPersonAlias(
            operation="add", identifier_kind="phone", value=OFFICE, namespace=SIGNATURE_NAMESPACE
        )
