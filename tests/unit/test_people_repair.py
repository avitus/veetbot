"""The one-time People directory repair keeps who the owner knows or writes to (ADR-0121)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.people import PublicPeopleService
from agent_core.application.people_repair import PeopleDirectoryRepair
from agent_core.domain.email import EmailRecord
from agent_core.domain.email_semantics import thread_source_key
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import (
    BeliefType,
    ConsolidationRun,
    MemoryAuthority,
    MemoryRecord,
    Sensitivity,
)
from agent_core.domain.people import (
    InteractionParticipant,
    PeopleEndpoint,
    PeopleImportJob,
    PeopleInteraction,
    PeopleOperation,
    PeopleSource,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    PersonMention,
    RelationshipAssertion,
)
from agent_core.domain.people_imports import PeopleImportScope
from agent_core.domain.people_sources import email_source_id
from agent_core.domain.people_views import UpdatePerson
from agent_core.memory.email_people import EmailPeopleFormationService
from agent_core.memory.formation import GovernedMemoryService
from tests.contract.memory_fixtures import memory, semantic_stack
from tests.contract.people_fixtures import PeopleFields
from tests.contract.people_mail_fixtures import (
    ALEX,
    OWNER,
    all_rows,
    colleague_fact,
    mail,
    seed_account,
)
from tests.contract.support import NOW, principal, session

MAILBOX = UUID(int=410)
OWNER_PRINCIPAL = principal().model_copy(update={"scopes": {"people.read", "people.write"}})


def _repair(factory: Any, clock: Any) -> PeopleDirectoryRepair:
    shared = SequenceIdFactory(UUID(int=value) for value in range(900_000, 901_000))
    return PeopleDirectoryRepair(
        factory,
        clock,
        memory_for=lambda owner: GovernedMemoryService(factory, clock, shared, owner),
        reproject=lambda owner, record: EmailPeopleFormationService(
            factory, clock, shared, owner, provider="people-repair", model="none"
        ).reproject_correspondence(record),
    )


def _fields() -> PeopleFields:
    return {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW - timedelta(days=10),
        "updated_at": NOW - timedelta(days=10),
    }


@dataclass
class Directory:
    """The directory formation@11 left behind: mail-body people beside real ones."""

    people: dict[str, UUID] = field(default_factory=dict)
    beliefs: dict[str, UUID] = field(default_factory=dict)
    mail_source: UUID = field(default_factory=uuid4)
    chat_source: UUID = field(default_factory=uuid4)
    meeting: UUID = field(default_factory=uuid4)
    audit_session: UUID = field(default_factory=uuid4)


async def _audit_session(factory: Any, directory: Directory) -> None:
    async with factory() as uow:
        await uow.sessions.create(
            session().model_copy(
                update={"id": directory.audit_session, "metadata": {"purpose": "people-management"}}
            )
        )


async def _legacy_directory(factory: Any) -> Directory:
    directory = Directory()
    common = _fields()
    position = iter(range(700, 800))

    def belief(label: str, subject: str, statement: str, *, mail: bool) -> MemoryRecord:
        belief_id = uuid4()
        directory.beliefs[label] = belief_id
        return memory(statement=statement).model_copy(
            update={
                "id": belief_id,
                "subject": subject,
                "belief_type": BeliefType.RELATIONSHIP,
                "authority": MemoryAuthority.INFERRED if mail else MemoryAuthority.USER,
                "source_session_id": MAILBOX if mail else session().id,
                "consolidation_policy_version": "email-semantic@2" if mail else "formation@11",
                "store_position": next(position),
            }
        )

    async with factory() as uow:
        await uow.people.put(
            PeopleSource(
                id=directory.mail_source,
                session_id=MAILBOX,
                event_sequence=1,
                source_kind="email",
                evidence_at=NOW - timedelta(days=5),
                account_id="work",
                thread_id="t-news",
                message_id="m-news",
                source_revision="a" * 64,
                **common,
            ),
            expected_revision=0,
        )
        await uow.people.put(
            PeopleSource(
                id=directory.chat_source,
                session_id=session().id,
                event_sequence=1,
                source_kind="owner",
                evidence_at=NOW - timedelta(days=5),
                source_revision="b" * 64,
                **common,
            ),
            expected_revision=0,
        )
        for label, name, state, pinned, source in (
            ("jensen", "Jensen Huang", "provisional", False, directory.mail_source),
            ("pronoun", "I", "provisional", False, directory.mail_source),
            ("self", "owner@example.test", "provisional", False, directory.mail_source),
            ("maya", "Maya", "provisional", False, directory.chat_source),
            ("jules", "Jules", "provisional", False, directory.chat_source),
            ("mom", "Mom", "provisional", False, directory.mail_source),
            ("kyrri", "Kyrri", "active", False, None),
            ("pat", "Pat", "provisional", True, directory.mail_source),
            ("correspondent", "Correspondent", "provisional", False, directory.mail_source),
        ):
            person = Person(
                id=uuid4(),
                display_name=name,
                state=state,  # type: ignore[arg-type]
                pinned=pinned,
                support_ids=[source] if source else [],
                **common,
            )
            directory.people[label] = person.id
            await uow.people.put(person, expected_revision=0)
        people = directory.people
        for label in ("jensen", "pronoun"):
            await uow.people.put(
                PersonMention(
                    id=uuid4(),
                    source_id=directory.mail_source,
                    person_id=people[label],
                    start=0,
                    end=5,
                    role="subject",
                    support_ids=[directory.mail_source],
                    **common,
                ),
                expected_revision=0,
            )
        rows: list[tuple[MemoryRecord, list[tuple[str, UUID]]]] = [
            (
                belief(
                    "jensen",
                    f"person:{people['jensen']}:Jensen Huang",
                    "Jensen Huang leads the keynote.",
                    mail=True,
                ),
                [("jensen", directory.mail_source)],
            ),
            (
                belief("jules", f"person:{people['jules']}:Jules", "Jules likes tea.", mail=False),
                [("jules", directory.chat_source)],
            ),
            (
                belief(
                    "shared",
                    f"person:{people['jensen']},{people['maya']}:Jensen and Maya",
                    "Jensen Huang and Maya spoke on a panel.",
                    mail=True,
                ),
                [("jensen", directory.mail_source), ("maya", directory.mail_source)],
            ),
        ]
        for record, links in rows:
            await uow.memories.upsert_belief(record)
            for label, source in links:
                await uow.people.put(
                    PersonMemoryLink(
                        id=uuid4(),
                        person_id=people[label],
                        belief_id=record.id,
                        role="subject",
                        support_ids=[source],
                        **common,
                    ),
                    expected_revision=0,
                )
        for label, predicate, statement, source, mail_derived in (
            ("maya", "sibling", "Maya is my sister.", directory.chat_source, False),
            ("mom", "parent", "Mom is the owner's mother.", directory.mail_source, True),
        ):
            record = belief(
                label + "-tie",
                f"person:{people[label]}:{label}",
                statement,
                mail=mail_derived,
            )
            await uow.memories.upsert_belief(record)
            await uow.people.put(
                RelationshipAssertion(
                    id=uuid4(),
                    subject=PeopleEndpoint(kind="person", id=people[label]),
                    object=PeopleEndpoint(kind="owner"),
                    predicate=predicate,  # type: ignore[arg-type]
                    belief_id=record.id,
                    support_ids=[source],
                    **common,
                ),
                expected_revision=0,
            )
            await uow.people.put(
                PersonMemoryLink(
                    id=uuid4(),
                    person_id=people[label],
                    belief_id=record.id,
                    role="subject",
                    support_ids=[source],
                    **common,
                ),
                expected_revision=0,
            )
        await uow.people.put(
            PeopleInteraction(
                id=uuid4(),
                channel="email",
                interaction_kind="exchange",
                attribution="observed",
                direction="outgoing",
                summary="Sent email",
                occurred_at=NOW - timedelta(days=5),
                participants=[
                    InteractionParticipant(person_id=people["correspondent"], role="recipient")
                ],
                support_ids=[directory.mail_source],
                **common,
            ),
            expected_revision=0,
        )
        await uow.people.put(
            PeopleInteraction(
                id=directory.meeting,
                channel="chat",
                interaction_kind="meeting",
                attribution="owner_reported",
                direction="reported",
                summary="Lunch",
                occurred_at=NOW - timedelta(days=5),
                participants=[
                    InteractionParticipant(person_id=people["maya"], role="participant"),
                    InteractionParticipant(person_id=people["jules"], role="mentioned"),
                ],
                support_ids=[directory.chat_source],
                **common,
            ),
            expected_revision=0,
        )
    await _audit_session(factory, directory)
    return directory


async def _stack() -> tuple[Any, Any]:
    factory, legacy, _source, _fact, _ = await semantic_stack(age=30)
    await seed_account(factory)
    return factory, legacy._clock


async def _names(factory: Any) -> set[str]:
    async with factory() as uow:
        rows = await uow.people.query(all_rows(["person"]))
    return {row.display_name for row in rows if isinstance(row, Person)}


async def test_preview_reports_candidates_and_writes_nothing() -> None:
    factory, clock = await _stack()
    directory = await _legacy_directory(factory)
    async with factory() as uow:
        before = (
            await uow.people.watermark(principal()),
            await uow.memories.head_position(principal()),
        )
    report = await _repair(factory, clock).run(OWNER_PRINCIPAL, confirm=False)
    assert not report.confirmed
    assert {(row.display_name, row.reason) for row in report.candidates} == {
        ("Jensen Huang", "unconfirmed"),
        ("I", "pronoun"),
        ("owner@example.test", "self"),
        ("Jules", "unconfirmed"),
        ("Mom", "unconfirmed"),
    }
    assert report.aliases_added == ["Kyrri"]
    async with factory() as uow:
        after = (
            await uow.people.watermark(principal()),
            await uow.memories.head_position(principal()),
        )
        assert await uow.events.list_after(directory.audit_session, 0, principal()) == []
    assert after == before


async def test_confirm_deletes_mail_subject_facts_and_unlinks_owner_facts_without_suppression() -> (
    None
):
    factory, clock = await _stack()
    directory = await _legacy_directory(factory)
    report = await _repair(factory, clock).run(
        OWNER_PRINCIPAL, confirm=True, session_id=directory.audit_session
    )
    assert report.confirmed
    assert {row.display_name for row in report.candidates} == {
        "Jensen Huang",
        "I",
        "owner@example.test",
        "Jules",
        "Mom",
    }
    assert (report.beliefs_deleted, report.beliefs_unlinked) == (2, 2)
    assert await _names(factory) == {"Maya", "Kyrri", "Pat", "Correspondent"}
    beliefs = directory.beliefs
    async with factory() as uow:
        for label in ("jensen", "mom-tie"):
            with pytest.raises(NotFoundError):
                await uow.memories.get(beliefs[label], principal())
        # Owner-stated facts stay; a fact shared with someone kept stays linked to them.
        for label in ("jules", "shared", "maya-tie"):
            await uow.memories.get(beliefs[label], principal())
        links = await uow.people.query(all_rows(["memory_link"]))
        assert {
            (row.person_id, row.belief_id) for row in links if isinstance(row, PersonMemoryLink)
        } == {
            (directory.people["maya"], beliefs["shared"]),
            (directory.people["maya"], beliefs["maya-tie"]),
        }
        mentions = await uow.people.query(all_rows(["mention"]))
        assert len(mentions) == 2
        assert all(isinstance(row, PersonMention) and row.person_id is None for row in mentions)
        # Removing people suppresses no source: the mail can still form facts.
        assert not await uow.people.source_suppressed(principal(), directory.mail_source)
        meeting = await uow.people.get(
            principal(), directory.meeting, ceiling=Sensitivity.RESTRICTED
        )
        assert isinstance(meeting, PeopleInteraction)
        assert [(p.person_id, p.role) for p in meeting.participants] == [
            (directory.people["maya"], "participant")
        ]
        aliases = await uow.people.query(
            all_rows(["identifier"]).model_copy(update={"person_id": directory.people["kyrri"]})
        )
        assert [
            (row.value, row.verification) for row in aliases if isinstance(row, PersonIdentifier)
        ] == [("Kyrri", "owner_confirmed")]
        audit = await uow.events.list_after(directory.audit_session, 0, principal())
    pruned = [event for event in audit if event.event_type == "people.directory_pruned"]
    assert len(pruned) == 5
    serialized = json.dumps([event.payload for event in pruned])
    assert not any(name in serialized for name in ("Jensen", "Jules", "Mom", "owner@example"))


async def test_owner_ties_and_owner_touched_people_are_kept() -> None:
    factory, clock = await _stack()
    directory = await _legacy_directory(factory)
    common = _fields()
    kept: dict[str, UUID] = {}
    async with factory() as uow:
        for label in ("confirmed", "renamed", "explicit", "met", "merge"):
            person = Person(
                id=uuid4(),
                display_name=label.title(),
                support_ids=[directory.mail_source],
                **common,
            )
            kept[label] = person.id
            await uow.people.put(person, expected_revision=0)
        await uow.people.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=kept["confirmed"],
                identifier_kind="email",
                namespace="owner",
                value="confirmed@example.test",
                context="owner",
                verification="owner_confirmed",
                valid_from=NOW - timedelta(days=10),
                **common,
            ),
            expected_revision=0,
        )
        # An explicit remember records its own formation run.
        run = ConsolidationRun(
            id=uuid4(),
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            trigger="explicit",
            scope="general",
            session_id=session().id,
            watermark_before=0,
            watermark_after=1,
            model="explicit",
            policy_version="formation@11",
            candidates_proposed=1,
            committed=1,
            reinforced=0,
            superseded=0,
            rejected=0,
            started_at=NOW - timedelta(days=10),
        )
        await uow.memories.record_consolidation(run)
        remembered = memory(statement="Explicit asked to be remembered.").model_copy(
            update={
                "id": uuid4(),
                "subject": f"person:{kept['explicit']}:Explicit",
                "formation_run_id": run.id,
                "store_position": 900,
            }
        )
        await uow.memories.upsert_belief(remembered)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                person_id=kept["explicit"],
                belief_id=remembered.id,
                support_ids=[directory.chat_source],
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
                summary="Coffee",
                participants=[InteractionParticipant(person_id=kept["met"], role="participant")],
                support_ids=[directory.chat_source],
                **common,
            ),
            expected_revision=0,
        )
        await uow.people.put(
            PeopleOperation(
                id=uuid4(),
                operation="merge",
                state="completed",
                person_ids=[kept["merge"]],
                expires_at=NOW,
                request_hash="c" * 64,
                **common,
            ),
            expected_revision=0,
        )
    await PublicPeopleService(factory, clock).update(
        OWNER_PRINCIPAL,
        kept["renamed"],
        UpdatePerson(session_id=session().id, expected_revision=1, display_name="Renamed Friend"),
        key="rename",
        ceiling=Sensitivity.SENSITIVE,
    )
    report = await _repair(factory, clock).run(
        OWNER_PRINCIPAL, confirm=True, session_id=directory.audit_session
    )
    assert not {row.person_id for row in report.candidates} & set(kept.values())
    names = await _names(factory)
    assert {"Confirmed", "Renamed Friend", "Explicit", "Met", "Merge", "Maya", "Pat"} <= names


async def test_email_derived_owner_relationship_is_not_an_owner_tie() -> None:
    factory, clock = await _stack()
    directory = await _legacy_directory(factory)
    report = await _repair(factory, clock).run(OWNER_PRINCIPAL, confirm=False)
    reasons = {row.display_name: row.reason for row in report.candidates}
    # Both are tied to the owner by a relationship; only the owner's own words count.
    assert reasons.get("Mom") == "unconfirmed"
    assert "Maya" not in reasons
    assert directory.people["maya"] not in {row.person_id for row in report.candidates}


async def test_backfill_reprojects_retained_mail_and_skips_bulk_old_and_erased_sources() -> None:
    from agent_core.domain.email import EmailAccount

    factory, legacy, _source, _fact, _ = await semantic_stack(age=30)
    clock = legacy._clock
    service = EmailPeopleFormationService(
        factory, clock, SequenceIdFactory(), principal(), provider="fake", model="scripted"
    )
    # Mail registered before this change never reached People: no account address yet.
    sources = {}
    for key, sender, to, labels, days, thread in (
        ("in", ALEX, None, None, 6, "t-alex"),
        ("reply", OWNER, ALEX, ["SENT"], 5, "t-alex"),
        ("bulk", "Newsletter <digest@news.test>", None, None, 4, "t-bulk"),
        ("old", OWNER, "Old Friend <old@example.test>", ["SENT"], 120, "t-old"),
        ("erased", OWNER, "Erased Person <erased@example.test>", ["SENT"], 3, "t-erased"),
    ):
        source = await mail(
            factory,
            message_id=f"m-{key}",
            sender=sender,
            to=to,
            labels=labels,
            days_ago=days,
            thread_id=thread,
        )
        await service.register_source(source)
        sources[key] = source
    assert await _names(factory) == set()
    await seed_account(factory)
    common = _fields()
    async with factory() as uow:
        # A later census marks the newsletter thread as bulk (ADR-0116).
        await uow.email.put(
            EmailRecord(
                kind="subscription_thread",
                key=thread_source_key("work", "t-bulk"),
                revision=1,
                payload={},
                **common,
            ),
            expected_revision=0,
        )
        erased = sources["erased"]
        sid = email_source_id(principal(), erased)
        await uow.people.put(
            PeopleSource(
                id=sid,
                session_id=erased.session_id,
                event_sequence=erased.source_event_sequence,
                source_kind="email",
                evidence_at=erased.sent_at,
                account_id="work",
                thread_id="t-erased",
                message_id="m-erased",
                source_revision="d" * 64,
                **common,
            ),
            expected_revision=0,
        )
        await uow.people.erase(principal(), [sid])
        account = await uow.email.get(principal(), "account", "work")
        assert account is not None
        assert EmailAccount.model_validate(account.payload).status == "syncing"
    audit = Directory()
    await _audit_session(factory, audit)
    report = await _repair(factory, clock).run(
        OWNER_PRINCIPAL, confirm=True, session_id=audit.audit_session
    )
    assert (report.retained_mail, report.mail_projected, report.mail_skipped) == (4, 2, 2)
    assert await _names(factory) == {"Alex Rivera"}
    async with factory() as uow:
        history = await uow.people.query(all_rows(["interaction"]))
        endpoints = await uow.people.query(all_rows(["identifier"]))
    assert sorted(row.direction for row in history if isinstance(row, PeopleInteraction)) == [
        "incoming",
        "outgoing",
    ]
    assert not [
        row
        for row in endpoints
        if isinstance(row, PersonIdentifier) and row.value.endswith("news.test")
    ]


async def test_repair_is_idempotent_and_later_formation_neither_recreates_nor_fails() -> None:
    import hashlib

    factory, legacy, source, _fact, _ = await semantic_stack(
        age=3, body="Alex is Maya's colleague."
    )
    clock = legacy._clock
    await seed_account(factory)
    service = EmailPeopleFormationService(
        factory, clock, SequenceIdFactory(), principal(), provider="fake", model="scripted"
    )
    common = _fields()
    async with factory() as uow:
        # formation@11 created both from this very message before ADR-0121.
        for name in ("Alex", "Maya"):
            person = Person(id=uuid4(), display_name=name, **common)
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="name",
                    namespace="owner",
                    value=name,
                    context="email:" + hashlib.sha256(source.sender.encode()).hexdigest(),
                    verification="contextual",
                    valid_from=NOW - timedelta(days=10),
                    **common,
                ),
                expected_revision=0,
            )
    [belief] = await service.form(source, [colleague_fact(source)])
    assert not belief.subject.startswith("person:unresolved:")
    audit = Directory()
    await _audit_session(factory, audit)
    repair = _repair(factory, clock)
    preview = await repair.run(OWNER_PRINCIPAL, confirm=False)
    # Deleting a fact resets the generated summary of the thread it came from.
    assert (preview.beliefs_deleted, preview.mail_threads_reset) == (1, 1)
    first = await repair.run(OWNER_PRINCIPAL, confirm=True, session_id=audit.audit_session)
    assert {row.display_name for row in first.candidates} == {"Alex", "Maya"}
    assert (first.beliefs_deleted, first.mail_threads_reset) == (1, 1)
    second = await repair.run(OWNER_PRINCIPAL, confirm=True, session_id=audit.audit_session)
    assert second.candidates == [] and second.beliefs_deleted == second.beliefs_unlinked == 0
    # Later mail naming the same people forms its fact unlinked and adds no one.
    later = await mail(
        factory,
        message_id="m-later",
        sender=source.sender,
        body="Alex is Maya's colleague.",
        days_ago=1,
        thread_id="t-later",
    )
    await service.register_source(later)
    [unlinked] = await service.form(later, [colleague_fact(later)])
    assert unlinked.subject.startswith("person:unresolved:")
    assert await _names(factory) == set()


async def test_repair_refuses_while_a_people_import_runs() -> None:
    factory, clock = await _stack()
    directory = await _legacy_directory(factory)
    async with factory() as uow:
        await uow.people.put(
            PeopleImportJob(
                id=uuid4(),
                state="running",
                scope=PeopleImportScope(
                    session_ids=[session().id],
                    since=NOW - timedelta(days=30),
                    until=NOW,
                    max_records=10,
                    max_cost_usd=Decimal("1"),
                ),
                audit_session_id=session().id,
                expires_at=NOW + timedelta(minutes=10),
                request_hash="a" * 64,
                implementation_sha256="b" * 64,
                **_fields(),
            ),
            expected_revision=0,
        )
    with pytest.raises(ConflictError, match="import"):
        await _repair(factory, clock).run(
            OWNER_PRINCIPAL, confirm=True, session_id=directory.audit_session
        )
    assert "Jensen Huang" in await _names(factory)


async def test_group_and_address_named_people_leave_whatever_their_history() -> None:
    """A team or an address mistaken for a person goes even though the owner wrote to it."""
    factory, clock = await _stack()
    directory = await _legacy_directory(factory)
    common = _fields()
    async with factory() as uow:
        for name in ("Investment Team", "iron@gracepres.test"):
            person = Person(
                id=uuid4(), display_name=name, support_ids=[directory.mail_source], **common
            )
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PeopleInteraction(
                    id=uuid4(),
                    channel="email",
                    interaction_kind="exchange",
                    attribution="observed",
                    direction="outgoing",
                    summary="Sent email",
                    occurred_at=NOW - timedelta(days=2),
                    participants=[InteractionParticipant(person_id=person.id, role="recipient")],
                    support_ids=[directory.mail_source],
                    **common,
                ),
                expected_revision=0,
            )
    report = await _repair(factory, clock).run(OWNER_PRINCIPAL, confirm=False)
    reasons = {row.display_name: row.reason for row in report.candidates}
    assert reasons["Investment Team"] == "group"
    assert reasons["iron@gracepres.test"] == "group"
    assert "Correspondent" not in reasons


async def test_group_named_people_the_owner_renamed_or_gave_an_address_stay() -> None:
    """The owner's own words keep a person whose name reads like a group (ADR-0125)."""
    factory, clock = await _stack()
    directory = await _legacy_directory(factory)
    common = _fields()
    renamed = Person(id=uuid4(), display_name="Sam", support_ids=[directory.mail_source], **common)
    addressed = Person(
        id=uuid4(), display_name="Dana (Finance)", support_ids=[directory.mail_source], **common
    )
    async with factory() as uow:
        await uow.people.put(renamed, expected_revision=0)
        await uow.people.put(addressed, expected_revision=0)
        await uow.people.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=addressed.id,
                identifier_kind="email",
                namespace="owner",
                value="dana@example.test",
                context="owner",
                verification="owner_confirmed",
                valid_from=NOW - timedelta(days=10),
                **common,
            ),
            expected_revision=0,
        )
    await PublicPeopleService(factory, clock).update(
        OWNER_PRINCIPAL,
        renamed.id,
        UpdatePerson(session_id=session().id, expected_revision=1, display_name="Sam from Legal"),
        key="rename-legal",
        ceiling=Sensitivity.SENSITIVE,
    )
    report = await _repair(factory, clock).run(
        OWNER_PRINCIPAL, confirm=True, session_id=directory.audit_session
    )
    assert not {row.person_id for row in report.candidates} & {renamed.id, addressed.id}
    assert {"Sam from Legal", "Dana (Finance)"} <= await _names(factory)
