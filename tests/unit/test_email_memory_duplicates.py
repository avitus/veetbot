"""ADR-0341: one fact in an email thread forms one memory, however often it is assessed or quoted.

Production on 2026-11-20 held 668 live email memories, and 271 of them repeated
a value another memory from the same thread already stated: 157 from the same
message assessed again in different words, the rest from other messages.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_core.domain.email import EmailMessage
from agent_core.domain.email_memory import (
    EmailMergeReport,
    first_class_statement,
    guess_statement,
    named_message,
    names_message,
    names_thread,
    quoted_text,
    restated,
    stated_value,
    value_key,
)
from agent_core.domain.email_semantics import EmailSemanticFact, semantic_source_key
from agent_core.domain.errors import AuthorizationError, ConflictError, NotFoundError
from agent_core.domain.memory import BeliefType, MemoryAuthority, MemoryRecord, MemoryStatus
from tests.contract.memory_fixtures import browse_query
from tests.contract.people_mail_fixtures import ALEX, correspondence_stack, mail, seed_account
from tests.contract.support import principal
from tests.unit.test_email_memory_standing import seed_thread

SENTENCE = "The Atlas board vote moved to Friday."
SAM = "Sam Okafor <sam@example.test>"


def fact_for(
    source: Any,
    *,
    predicate: str = "moved to",
    quote: str = SENTENCE,
    subject: str = "Atlas board vote",
    value: str = "Friday",
) -> EmailSemanticFact:
    return EmailSemanticFact(
        message_id=source.message_id,
        quote=quote,
        belief_type=BeliefType.FACT,
        subject=subject,
        predicate=predicate,
        value=value,
        confidence=0.4,
    )


def _stored(source: Any) -> EmailMessage:
    """The message as the mailbox projection holds it, body included."""
    return EmailMessage(
        id=source.message_id, sender=source.sender, sent_at=source.sent_at, body=source.body
    )


async def _email_memories(factory: Any, *, live: bool = True) -> list[MemoryRecord]:
    async with factory() as uow:
        rows = await uow.memories.browse(
            browse_query(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                statuses=tuple(MemoryStatus),
            )
        )
    rows = [row for row in rows if row.consolidation_policy_version == "email-semantic@2"]
    live_statuses = {MemoryStatus.ACTIVE, MemoryStatus.PROVISIONAL}
    return [row for row in rows if not live or row.status in live_statuses]


def test_quoted_text_is_marked_lines_or_a_long_sentence_from_an_earlier_message() -> None:
    reply = "Works for me.\n\nOn Tue, Alex wrote:\n> The Atlas board vote moved to\n> Friday.\n"
    assert quoted_text(SENTENCE, reply, [])
    assert not quoted_text("Works for me.", reply, [])
    # Outlook puts the earlier text under a header with no marks at all.
    outlook = f"Works for me.\n\nFrom: Alex\nSent: Tuesday\n\n{SENTENCE}\n"
    assert not quoted_text(SENTENCE, outlook, [])
    assert quoted_text(SENTENCE, outlook, [f"Hello all,\n{SENTENCE}\nAlex"])
    # Two people can each write a short sentence; only its line marks say it is quoted.
    assert not quoted_text("Approved", "Approved\n\n> Approved", ["Approved"])
    assert quoted_text("Approved", "Fine by me.\n\n> Approved", [])
    assert stated_value('Alex said by email on 2026-07-22 that Atlas vote moved to: "Friday".') == (
        "Friday"
    )
    assert stated_value("The Atlas board vote is on Tuesday.") is None


async def test_a_message_assessed_again_in_other_words_forms_no_second_memory() -> None:
    """A new message restarts the thread's analysis, so each older message is assessed
    again and the model words the same fact differently every time."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE)
    [first] = await service.form(source, [fact_for(source)])
    again = await service.form(source, [fact_for(source, predicate="rescheduled_to")])
    reworded = fact_for(source, predicate="new_date_reported_by_Alex", quote="moved to Friday")
    assert again == [first] and await service.form(source, [reworded]) == [first]
    assert await _email_memories(factory) == [first]
    # A different value, or a different subject, is a different fact.
    other = await service.form(
        source, [fact_for(source, subject="Atlas board", quote="The Atlas board", value="Atlas")]
    )
    assert len(other) == 1 and other[0].id != first.id


async def test_a_fact_whose_memory_ended_is_not_formed_again_in_other_words() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE)
    [first] = await service.form(source, [fact_for(source)])
    async with factory() as uow:
        expired = await uow.memories.reinforce(
            first.model_copy(update={"status": MemoryStatus.EXPIRED})
        )
    assert await service.form(source, [fact_for(source, predicate="rescheduled_to")]) == [expired]
    assert await _email_memories(factory, live=False) == [expired]
    # A deletion goes further and has always closed its source to formation.
    await service._governed.delete(first.id)
    with pytest.raises(ConflictError, match="excluded"):
        await service.form(source, [fact_for(source, predicate="another_wording")])


async def test_a_reply_that_quotes_a_fact_forms_no_memory_and_renews_nothing() -> None:
    """The quoted sentence is a substring of the reply, so it grounds there too."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    original = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE, days_ago=5)
    reply = await mail(
        factory,
        message_id="m-2",
        sender=SAM,
        days_ago=2,
        body=f"Works for me.\n\nOn Tue, Alex Rivera <alex@example.test> wrote:\n> {SENTENCE}\n",
    )
    [first] = await service.form(original, [fact_for(original)])
    # The model reports the quoted sentence as the replier's claim, in the same or other words.
    assert await service.form(reply, [fact_for(reply)]) == []
    assert await service.form(reply, [fact_for(reply, predicate="date_reported_by_Sam")]) == []
    # Nothing new was said, so the original memory's evidence date has not moved.
    assert await _email_memories(factory) == [first]


async def test_a_reply_without_quote_marks_is_checked_against_the_earlier_messages() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    original = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE, days_ago=5)
    reply = await mail(
        factory,
        message_id="m-2",
        sender=SAM,
        days_ago=2,
        body=f"Works for me.\n\nFrom: Alex Rivera\nSent: Tuesday\nSubject: Atlas\n\n{SENTENCE}\n",
    )
    forwarded = await mail(
        factory,
        message_id="m-3",
        sender=SAM,
        days_ago=1,
        body=(
            "FYI\n\n---------- Forwarded message ---------\nThe Borealis audit closes on the ninth."
        ),
    )
    await seed_thread(
        factory, priority=0.2, messages=[_stored(original), _stored(reply), _stored(forwarded)]
    )
    [first] = await service.form(original, [fact_for(original)])
    assert await service.form(reply, [fact_for(reply, predicate="date_reported_by_Sam")]) == []
    assert await _email_memories(factory) == [first]
    # Forwarded content is in no earlier message of the thread, so it is new and forms.
    [new] = await service.form(
        forwarded,
        [
            fact_for(
                forwarded,
                quote="The Borealis audit closes on the ninth.",
                subject="Borealis audit",
                predicate="closes on",
                value="the ninth",
            )
        ],
    )
    assert new.id != first.id


async def test_two_people_who_each_write_a_short_sentence_each_form_a_memory() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    first = await mail(factory, message_id="m-1", sender=ALEX, body="Approved", days_ago=5)
    second = await mail(factory, message_id="m-2", sender=SAM, body="Approved", days_ago=2)
    await seed_thread(factory, priority=0.2, messages=[_stored(first), _stored(second)])

    def approval(source: Any, who: str) -> EmailSemanticFact:
        return fact_for(source, quote="Approved", subject=who, predicate="reply", value="Approved")

    [one] = await service.form(first, [approval(first, "Alex Rivera")])
    [two] = await service.form(second, [approval(second, "Sam Okafor")])
    assert one.id != two.id and len(await _email_memories(factory)) == 2


async def test_a_later_message_that_restates_the_fact_renews_the_one_memory() -> None:
    """Only the quoted part of a reply is somebody else's sentence."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    original = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE, days_ago=5)
    own_words = "Confirming the Atlas vote is now Friday."
    reply = await mail(
        factory,
        message_id="m-2",
        sender=SAM,
        days_ago=2,
        body=f"{own_words}\n\nOn Tue, Alex Rivera <alex@example.test> wrote:\n> {SENTENCE}\n",
    )
    [first] = await service.form(original, [fact_for(original)])
    [renewed] = await service.form(
        reply, [fact_for(reply, quote=own_words, predicate="confirmed_date_reported_by_Sam")]
    )
    # The predicate differs; the subject and the value say it is the same fact.
    assert renewed.id == first.id and renewed.last_evidence_at == reply.sent_at
    assert renewed.statement == first.statement
    assert [row.id for row in await _email_memories(factory)] == [first.id]


def test_a_value_keeps_the_punctuation_that_changes_it() -> None:
    """Case, spacing, wrapping quotes and a closing full stop are wording; a sign is the value."""
    assert value_key("Friday.") == value_key(' "friday" ') == value_key("FRIDAY") == "friday"
    assert value_key("-5 °C") != value_key("5 °C")
    assert value_key("3.5%") != value_key("3-5%") != value_key("35%")
    assert value_key("$5") != value_key("€5") and value_key("A+") != value_key("A-")
    assert value_key("10:30") != value_key("10/30") and value_key('5"') != value_key("5")
    assert value_key("...") == "..."


async def test_two_values_that_differ_by_a_sign_are_two_facts() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    reading = "The freezer reads -5 °C and the fridge reads 5 °C."
    source = await mail(factory, message_id="m-1", sender=ALEX, body=reading)

    def temperature(value: str) -> EmailSemanticFact:
        return fact_for(source, quote=reading, subject="Cold room", predicate="reads", value=value)

    [freezer] = await service.form(source, [temperature("-5 °C")])
    [fridge] = await service.form(source, [temperature("5 °C")])
    assert fridge.id != freezer.id and len(await _email_memories(factory)) == 2
    assert (await service.merge_duplicates(apply=True)).superseded == 0


async def test_a_value_is_read_past_a_subject_that_holds_a_quoted_name() -> None:
    """The statement ends with the value in quotes; a subject may hold quotes of its own."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    subject = 'Project: "Atlas"'
    first_body = 'Project: "Atlas" is due on Friday.'
    original = await mail(factory, message_id="m-1", sender=ALEX, body=first_body, days_ago=5)
    restating = "Confirming the Atlas project is due Friday."
    reply = await mail(factory, message_id="m-2", sender=SAM, body=restating, days_ago=2)
    [first] = await service.form(
        original, [fact_for(original, quote=first_body, subject=subject, predicate="is due on")]
    )
    assert first.statement.endswith('reports Project: "Atlas" is due on: "Friday".')
    [renewed] = await service.form(
        reply, [fact_for(reply, quote=restating, subject=subject, predicate="due_date")]
    )
    assert renewed.id == first.id and renewed.last_evidence_at == reply.sent_at
    assert stated_value(first.statement, first.subject) == "Friday"
    # A value may hold a quotation too, and is read whole.
    said = 'Alex said by email on 2026-07-22 that Board reply: "He said: "no"".'
    assert stated_value(said, "Board") == 'He said: "no"'


def test_a_first_class_statement_is_restated_whatever_its_senders_name_begins_with() -> None:
    """A display name may begin "Email from"; only the whole record line makes a guess."""
    sender, claim = "Email from Alex <alex@example.com>", 'Atlas vote moved to: "Friday".'

    def reword(
        statement: str, message_id: str, *, first_class: bool, direct: bool | None = None
    ) -> str | None:
        return restated(
            statement,
            account_id="work",
            thread_id="t-1",
            message_id=message_id,
            first_class=first_class,
            direct=direct,
        )

    plain = first_class_statement(sender, "2026-07-22", claim)
    guess = guess_statement(sender, "2026-07-22", "work", "t-1", "m-1", claim)
    assert reword(plain, "m-1", first_class=False) == guess
    assert reword(guess, "m-1", first_class=True) == plain
    # A guess that names another message is still left alone, in either direction.
    assert reword(guess, "m-2", first_class=True) is None
    assert reword(guess, "m-2", first_class=False) is None
    about_mail = guess_statement(
        "Sam <sam@example.test>",
        "2026-07-22",
        "work",
        "t-1",
        "m-1",
        'Alex said by email on 2026-07-20 that the vote moved to: "Friday".',
    )
    assert reword(about_mail, "m-2", first_class=True) is None
    # The email its claim quotes is part of the claim, never this guess's own sender.
    assert reword(about_mail, "m-2", first_class=False) is None
    assert reword(about_mail, "m-2", first_class=False, direct=False) is None
    assert reword(about_mail, "m-1", first_class=False) == about_mail

    # A display name can hold a whole record line. The words alone then read as a
    # guess for another message; the row's own standing says which wording it is in.
    crafted = (
        "Email from A on 2026-01-01 (mailbox work, thread t-9, message m-9) reports x "
        "<a@example.com>"
    )
    held = first_class_statement(crafted, "2026-07-22", claim)
    assert reword(held, "m-1", first_class=False) is None
    assert reword(held, "m-1", first_class=False, direct=True) == guess_statement(
        crafted, "2026-07-22", "work", "t-1", "m-1", claim
    )
    # A row that is already a guess is never read as a plain statement.
    assert reword(plain, "m-1", first_class=False, direct=False) is None

    # The display name may hold the record line of this very message. A first-class
    # row is in the plain wording, so nothing in its sender's name is read as a header.
    same = (
        "Email from A on 2026-01-01 (mailbox work, thread t-1, message m-1) reports x "
        "<a@example.com>"
    )
    own = first_class_statement(same, "2026-07-22", claim)
    assert reword(own, "m-1", first_class=False, direct=True) == guess_statement(
        same, "2026-07-22", "work", "t-1", "m-1", claim
    )


def test_a_record_line_is_told_by_its_own_header_never_by_one_its_claim_quotes() -> None:
    """A claim can quote another email's record line. The identifiers that say which
    message and thread formed a guess are the ones in its own header."""
    quoted = guess_statement(
        "Alex", "2026-07-20", "work", "t-9", "m-9", 'the vote moved to: "Friday".'
    )
    quoting = guess_statement(
        "Sam <sam@example.test>", "2026-07-22", "work", "t-1", "m-1", f'Alex wrote: "{quoted}".'
    )
    assert names_message(quoting, "work", "t-1", "m-1") and names_thread(quoting, "work", "t-1")
    assert not names_message(quoting, "work", "t-9", "m-9")
    assert not names_thread(quoting, "work", "t-9")
    # Another message of the header's own thread is not this one either.
    assert not names_message(quoting, "work", "t-1", "m-9")
    assert not names_thread(quoting, "work", "t-")
    # A first-class statement names no message, whatever its claim quotes.
    plain = first_class_statement(
        "Sam <sam@example.test>", "2026-07-22", f'Alex wrote: "{quoted}".'
    )
    assert not names_message(plain, "work", "t-9", "m-9")
    assert not names_thread(plain, "work", "t-9")
    assert named_message(quoting) == ("work", "t-1", "m-1") and named_message(plain) is None

    # A display name is the sender's to choose and can hold a record line too. The words
    # alone then end the header early. Read against the stored message's own sender, the
    # header is the whole of it, and another message's sender does not open the line.
    crafted = "A on 2026-01-01 (mailbox work, thread t-9, message m-9) reports x <a@example.com>"
    line = guess_statement(crafted, "2026-07-22", "work", "t-1", "m-1", 'the vote: "Friday".')
    assert names_message(line, "work", "t-1", "m-1", crafted)
    assert not names_message(line, "work", "t-9", "m-9", crafted)
    assert not names_message(line, "work", "t-9", "m-9", "Sam <sam@example.test>")
    assert not names_message(line, "work", "t-1", "m-1", "Sam <sam@example.test>")

    def promote(thread_id: str, message_id: str, sender: str) -> str | None:
        return restated(
            line,
            account_id="work",
            thread_id=thread_id,
            message_id=message_id,
            first_class=True,
            direct=False,
            sender=sender,
        )

    assert promote("t-1", "m-1", crafted) == first_class_statement(
        crafted, "2026-07-22", 'the vote: "Friday".'
    )
    # A promotion never takes its sender from words the stored sender does not open.
    assert promote("t-1", "m-1", "Sam <sam@example.test>") is None
    assert promote("t-9", "m-9", "Sam <sam@example.test>") is None


def test_a_value_is_read_past_the_subject_a_person_linked_memory_names() -> None:
    """A memory linked to a person is stored under `person:<who>:<subject>`, and its
    statement names the subject alone. A quotation in that subject is not the value."""
    claim = 'Project: "Atlas" is due on: "Friday".'
    plain = first_class_statement("Alex <alex@example.test>", "2026-07-22", claim)
    guess = guess_statement("Alex <alex@example.test>", "2026-07-22", "work", "t-1", "m-1", claim)
    who = "6f1c0f3a-5a50-4b77-9d0f-2f2f8d1c7e11"
    for stored_subject in (
        f'person:unresolved:{who}:Project: "Atlas"',
        f'person:{who}:Project: "Atlas"',
        f'person:{who},{who}:Project:  "Atlas"',
    ):
        assert stated_value(plain, stored_subject) == "Friday"
        assert stated_value(guess, stored_subject) == "Friday"
    # The value's own quotation is still read whole, under a person as under a subject.
    said = first_class_statement("Alex", "2026-07-22", 'Board reply: "He said: "no"".')
    assert stated_value(said, f"person:{who}:Board") == 'He said: "no"'
    # A stored subject that names something else falls back to the claim's first quotation.
    assert stated_value(said, f"person:{who}:Budget") == 'He said: "no"'


async def test_a_person_linked_fact_assessed_again_in_other_words_forms_no_second_memory() -> None:
    """The record of a message read before fact identities were kept lists only its
    memories, so the same fact is recognised by the value its statement holds."""
    from tests.contract.people_mail_fixtures import colleague_fact

    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-1", sender=ALEX, body="Alex is Maya's colleague.")
    subject = 'Project: "Atlas" lead'

    def fact(predicate: str) -> EmailSemanticFact:
        return colleague_fact(source).model_copy(
            update={"subject": subject, "predicate": predicate}
        )

    [first] = await service.form(source, [fact("is")])
    assert first.subject.startswith("person:unresolved:") and first.subject.endswith(subject)
    assert stated_value(first.statement, first.subject) == "Maya's colleague"
    key = semantic_source_key(source.account_id, source.provider_thread_id, source.message_id)
    async with factory() as uow:
        record = await uow.email.get(principal(), "semantic_source", key)
        assert record is not None
        await uow.email.put(
            record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "payload": {**record.payload, "facts": {}},
                }
            ),
            expected_revision=record.revision,
        )
    assert await service.form(source, [fact("works_as")]) == [first]
    assert await _email_memories(factory) == [first]


async def _copy(
    factory: Any, source: Any, original: MemoryRecord, number: int, **update: object
) -> MemoryRecord:
    """A copy as production holds them: another row for a fact, listed by its source
    message and worded as that message's own record line."""
    named = (
        f"(mailbox {source.account_id}, thread {source.provider_thread_id}, "
        f"message {source.message_id}) reports"
    )
    statement = re.sub(
        r"\(mailbox .+?, thread .+?, message .+?\) reports", named, original.statement
    )
    async with factory() as uow:
        copy: MemoryRecord = await uow.memories.upsert_belief(
            original.model_copy(
                update={
                    "id": UUID(int=9_000 + number),
                    "statement": statement.replace("moved to:", f"wording_{number}:"),
                    "valid_from": source.sent_at,
                    "last_evidence_at": source.sent_at,
                    "store_position": await uow.memories.next_position(),
                    **update,
                }
            )
        )
        key = semantic_source_key(source.account_id, source.provider_thread_id, source.message_id)
        record = await uow.email.get(principal(), "semantic_source", key)
        assert record is not None
        await uow.email.put(
            record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "payload": {
                        **record.payload,
                        "memory_ids": [*record.payload["memory_ids"], str(copy.id)],
                    },
                }
            ),
            expected_revision=record.revision,
        )
    return copy


async def test_the_merge_keeps_the_earliest_memory_of_a_fact_and_supersedes_its_copies() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    first_mail = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE, days_ago=6)
    later_mail = await mail(
        factory, message_id="m-2", sender=SAM, body=f"As Alex said: {SENTENCE}", days_ago=2
    )
    elsewhere = await mail(
        factory, message_id="m-9", sender=ALEX, body=SENTENCE, days_ago=4, thread_id="t-other"
    )
    [kept] = await service.form(first_mail, [fact_for(first_mail)])
    await service.register_source(later_mail)
    [other_thread] = await service.form(elsewhere, [fact_for(elsewhere, subject="Atlas vote")])
    same_message = await _copy(factory, first_mail, kept, 1)
    later = await _copy(factory, later_mail, kept, 2)
    other_subject = await _copy(factory, later_mail, kept, 3, subject="person:unresolved:sam")
    owned = await _copy(
        factory, later_mail, kept, 4, authority=MemoryAuthority.USER, status=MemoryStatus.ACTIVE
    )

    preview = await service.merge_duplicates(apply=False)
    assert preview == EmailMergeReport(
        applied=False, examined=5, facts=1, superseded=2, other_subjects=1
    )
    assert len(await _email_memories(factory)) == 6

    applied = await service.merge_duplicates(apply=True)
    assert applied.model_dump(exclude={"applied"}) == preview.model_dump(exclude={"applied"})
    async with factory() as uow:
        rows = {
            row.id: row
            for row in [
                await uow.memories.get(item.id, principal())
                for item in (kept, same_message, later, other_subject, owned, other_thread)
            ]
        }
        events = await uow.events.list_after(kept.source_session_id, 0, principal())
    # The memory kept is the same row in the same words, with its copies' later evidence.
    evidence = {"last_evidence_at", "evidence_count", "updated_at"}
    assert rows[kept.id].model_dump(exclude=evidence) == kept.model_dump(exclude=evidence)
    assert rows[kept.id].last_evidence_at == later_mail.sent_at
    for copy in (same_message, later):
        merged = rows[copy.id]
        assert merged.status is MemoryStatus.SUPERSEDED and merged.superseded_by == kept.id
        assert merged.valid_to is not None and merged.store_position > copy.store_position
    # A copy under another subject, the owner's own memory and another thread are left alone.
    assert rows[other_subject.id] == other_subject and rows[owned.id] == owned
    assert rows[other_thread.id] == other_thread
    [audit] = [event for event in events if event.event_type == "email.memory.merged"]
    assert audit.payload == {
        "merged": [
            {"kept": str(kept.id), "superseded": sorted([str(same_message.id), str(later.id)])}
        ]
    }
    again = await service.merge_duplicates(apply=True)
    assert (again.facts, again.superseded) == (0, 0)
    # A superseded copy's message does not form the fact again, in any wording: it
    # answers with a memory of the fact it already lists, the copy or the one kept.
    result = await service.form(later_mail, [fact_for(later_mail, predicate="as_alex_said")])
    assert len(result) == 1 and result[0].id in {kept.id, later.id}
    assert len(await _email_memories(factory)) == 4


async def test_the_merge_gives_the_memory_kept_its_copies_later_evidence_and_retention() -> None:
    """The earliest memory stays, and must not expire while a copy's later evidence holds."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    first_mail = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE, days_ago=29)
    later_mail = await mail(
        factory, message_id="m-2", sender=SAM, body=f"As Alex said: {SENTENCE}", days_ago=2
    )
    [kept] = await service.form(first_mail, [fact_for(first_mail)])
    # Kept thirty days from an email twenty-nine days old: it expires tomorrow.
    assert kept.expires_at == first_mail.sent_at + timedelta(days=30)
    await service.register_source(later_mail)
    year = later_mail.sent_at + timedelta(days=365)
    later = await _copy(factory, later_mail, kept, 2, expires_at=year)

    assert (await service.merge_duplicates(apply=False)).superseded == 1
    async with factory() as uow:
        assert await uow.memories.get(kept.id, principal()) == kept
    assert (await service.merge_duplicates(apply=True)).superseded == 1
    async with factory() as uow:
        survivor = await uow.memories.get(kept.id, principal())
        merged = await uow.memories.get(later.id, principal())
    assert merged.status is MemoryStatus.SUPERSEDED and merged.superseded_by == kept.id
    assert survivor.statement == kept.statement and survivor.status is kept.status
    assert survivor.valid_from == first_mail.sent_at
    assert survivor.last_evidence_at == later_mail.sent_at
    assert survivor.evidence_count == kept.evidence_count + 1
    assert survivor.expires_at == year

    # The later message now answers for the memory's retention, so a re-rating of
    # its thread still reaches the memory kept.
    await seed_thread(factory, priority=0.9, messages=[_stored(first_mail), _stored(later_mail)])
    assert (await service.rerate_thread("work", "t-mail")).by_retention == {"no_expiry": 1}
    async with factory() as uow:
        assert (await uow.memories.get(kept.id, principal())).expires_at is None
    assert (await service.merge_duplicates(apply=True)).superseded == 0


async def test_the_merge_counts_a_memory_only_in_the_thread_that_formed_it() -> None:
    """A message can renew or find a memory another thread formed, and then lists it.
    The merge works within one thread: that listing makes the memory no member of it."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    in_a = await mail(
        factory, message_id="m-a", sender=ALEX, body=SENTENCE, days_ago=6, thread_id="t-a"
    )
    earlier_b = await mail(
        factory, message_id="m-b0", sender=SAM, body="A note.", days_ago=9, thread_id="t-b"
    )
    confirming = "Confirming the Atlas vote is now Friday."
    renewing_b = await mail(
        factory, message_id="m-b1", sender=SAM, body=confirming, days_ago=2, thread_id="t-b"
    )
    agreeing = "Yes, Friday is right for the Atlas vote."
    seeing_b = await mail(
        factory, message_id="m-b2", sender=SAM, body=agreeing, days_ago=4, thread_id="t-b"
    )
    [formed_in_a] = await service.form(in_a, [fact_for(in_a)])
    # Thread B renews the memory thread A formed, and an older message of B then finds it.
    [renewed] = await service.form(renewing_b, [fact_for(renewing_b, quote=confirming)])
    [found] = await service.form(seeing_b, [fact_for(seeing_b, quote=agreeing)])
    assert renewed.id == found.id == formed_in_a.id
    # Thread B also holds its own, earlier memory of the fact from before roles were kept.
    await service.register_source(earlier_b)
    own_b = await _copy(factory, earlier_b, formed_in_a, 1)
    assert own_b.valid_from < formed_in_a.valid_from

    for apply in (False, True):
        report = await service.merge_duplicates(apply=apply)
        assert report == EmailMergeReport(applied=apply, examined=2)
    assert {row.id for row in await _email_memories(factory)} == {formed_in_a.id, own_b.id}

    # A listing with no role, as a record written before roles were kept holds it, is
    # no membership either: the memory's own record line names the thread that formed it.
    key = semantic_source_key("work", "t-b", "m-b1")
    async with factory() as uow:
        record = await uow.email.get(principal(), "semantic_source", key)
        assert record is not None
        facts = {
            name: value
            for name, value in record.payload["facts"].items()
            if not name.startswith(("renewed:", "seen:"))
        }
        await uow.email.put(
            record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "payload": {**record.payload, "facts": facts},
                }
            ),
            expected_revision=record.revision,
        )
    assert await service.merge_duplicates(apply=True) == EmailMergeReport(applied=True, examined=2)
    assert {row.id for row in await _email_memories(factory)} == {formed_in_a.id, own_b.id}


async def test_the_merge_leaves_a_copy_whose_latest_evidence_it_cannot_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The merge reads which messages list each memory before it takes its locks. A copy
    renewed in between has later evidence from a message that reading never saw."""
    from agent_core.memory.email_people import EmailPeopleFormationService

    factory, service = await correspondence_stack()
    await seed_account(factory)
    first_mail = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE, days_ago=9)
    copy_mail = await mail(factory, message_id="m-2", sender=SAM, body="A note.", days_ago=6)
    confirming = "Confirming the Atlas vote is now Friday."
    late_mail = await mail(factory, message_id="m-3", sender=SAM, body=confirming, days_ago=1)
    [kept] = await service.form(first_mail, [fact_for(first_mail)])
    await service.register_source(copy_mail)
    copy = await _copy(factory, copy_mail, kept, 2)
    other = EmailPeopleFormationService(
        factory, service._clock, service._ids, principal(), provider="fake", model="scripted"
    )
    opened = 0

    @asynccontextmanager
    async def racing() -> Any:
        nonlocal opened
        opened += 1
        if opened == 2:
            # After the listing was read, before this thread's merge: a new message
            # restates the fact and renews the newest memory of it, the copy.
            [renewed] = await other.form(late_mail, [fact_for(late_mail, quote=confirming)])
            assert renewed.id == copy.id and renewed.last_evidence_at == late_mail.sent_at
        async with factory() as uow:
            yield uow

    monkeypatch.setattr(service, "_uow_factory", racing)
    report = await service.merge_duplicates(apply=True)
    monkeypatch.undo()
    # Nothing that reading saw can answer for the copy's new evidence, so it stays.
    assert (report.facts, report.superseded) == (0, 0)
    async with factory() as uow:
        assert await uow.memories.get(kept.id, principal()) == kept
        assert (await uow.memories.get(copy.id, principal())).status is MemoryStatus.PROVISIONAL

    # The next run reads the renewing message's listing and carries its evidence over.
    assert (await service.merge_duplicates(apply=False)).superseded == 1
    assert (await service.merge_duplicates(apply=True)).superseded == 1
    async with factory() as uow:
        survivor = await uow.memories.get(kept.id, principal())
        merged = await uow.memories.get(copy.id, principal())
        linked = await uow.email.get(
            principal(), "semantic_source", semantic_source_key("work", "t-mail", "m-3")
        )
    assert merged.status is MemoryStatus.SUPERSEDED and merged.superseded_by == kept.id
    assert survivor.last_evidence_at == late_mail.sent_at
    assert linked is not None and linked.payload["facts"][f"renewed:{kept.id}"] == str(kept.id)
    assert (await service.merge_duplicates(apply=True)).superseded == 0


async def test_the_merge_checks_each_later_copy_before_superseding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two copies hold later evidence than the memory kept. One was renewed after the
    listing was read, by a message older than the other copy's evidence: nothing that
    reading saw answers for it, whatever answers for the newest copy."""
    from agent_core.memory.email_people import EmailPeopleFormationService

    factory, service = await correspondence_stack()
    await seed_account(factory)
    first_mail = await mail(factory, message_id="m-1", sender=ALEX, body=SENTENCE, days_ago=9)
    newest_mail = await mail(factory, message_id="m-2", sender=SAM, body="A note.", days_ago=3)
    middle_mail = await mail(factory, message_id="m-3", sender=SAM, body="A memo.", days_ago=7)
    confirming = "Confirming the Atlas vote is now Friday."
    late_mail = await mail(factory, message_id="m-4", sender=SAM, body=confirming, days_ago=5)
    [kept] = await service.form(first_mail, [fact_for(first_mail)])
    await service.register_source(newest_mail)
    await service.register_source(middle_mail)
    newest = await _copy(factory, newest_mail, kept, 2)
    # Stored last, so it is the memory of the fact a later message renews.
    middle = await _copy(factory, middle_mail, kept, 3)
    other = EmailPeopleFormationService(
        factory, service._clock, service._ids, principal(), provider="fake", model="scripted"
    )
    opened = 0

    @asynccontextmanager
    async def racing() -> Any:
        nonlocal opened
        opened += 1
        if opened == 2:
            [renewed] = await other.form(late_mail, [fact_for(late_mail, quote=confirming)])
            assert renewed.id == middle.id and renewed.last_evidence_at == late_mail.sent_at
        async with factory() as uow:
            yield uow

    monkeypatch.setattr(service, "_uow_factory", racing)
    report = await service.merge_duplicates(apply=True)
    monkeypatch.undo()
    # The newest copy's message was listed and answers for it; the renewed copy waits.
    assert (report.facts, report.superseded) == (1, 1)
    async with factory() as uow:
        survivor = await uow.memories.get(kept.id, principal())
        merged = await uow.memories.get(newest.id, principal())
        waiting = await uow.memories.get(middle.id, principal())
    assert merged.status is MemoryStatus.SUPERSEDED and merged.superseded_by == kept.id
    assert survivor.last_evidence_at == newest_mail.sent_at
    assert waiting.status is MemoryStatus.PROVISIONAL and waiting.superseded_by is None
    assert waiting.last_evidence_at == late_mail.sent_at

    # The next run reads the renewing message's listing and places the copy.
    assert (await service.merge_duplicates(apply=True)).superseded == 1
    async with factory() as uow:
        placed = await uow.memories.get(middle.id, principal())
        survivor = await uow.memories.get(kept.id, principal())
    assert placed.status is MemoryStatus.SUPERSEDED and placed.superseded_by == kept.id
    assert survivor.last_evidence_at == newest_mail.sent_at


async def test_the_merge_appends_one_event_for_a_thread_however_many_facts_it_merges() -> None:
    """The audit trail holds one content-free event per thread a merge changed, naming
    every fact it merged there; a preview appends none."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    due = "The diligence package is due Monday."
    first_mail = await mail(
        factory, message_id="m-1", sender=ALEX, body=f"{SENTENCE} {due}", days_ago=6
    )
    later_mail = await mail(factory, message_id="m-2", sender=SAM, body="A note.", days_ago=2)
    [vote, package] = await service.form(
        first_mail,
        [
            fact_for(first_mail),
            fact_for(
                first_mail,
                quote=due,
                subject="Diligence package",
                predicate="is due",
                value="Monday",
            ),
        ],
    )
    await service.register_source(later_mail)
    vote_copy = await _copy(factory, later_mail, vote, 1)
    package_copy = await _copy(factory, later_mail, package, 2)

    async def audits() -> list[Any]:
        async with factory() as uow:
            events = await uow.events.list_after(vote.source_session_id, 0, principal())
        return [event for event in events if event.event_type == "email.memory.merged"]

    preview = await service.merge_duplicates(apply=False)
    assert (preview.facts, preview.superseded) == (2, 2) and await audits() == []
    applied = await service.merge_duplicates(apply=True)
    assert (applied.facts, applied.superseded) == (2, 2)
    merged = await audits()
    assert len(merged) == 1
    assert merged[0].payload == {
        "merged": sorted(
            [
                {"kept": str(vote.id), "superseded": [str(vote_copy.id)]},
                {"kept": str(package.id), "superseded": [str(package_copy.id)]},
            ],
            key=lambda group: str(group["kept"]),
        )
    }
    assert (await service.merge_duplicates(apply=True)).superseded == 0
    assert len(await audits()) == 1


async def test_the_merge_command_previews_unless_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_core.cli import main

    owner = principal()
    calls: list[tuple[object, bool]] = []

    async def merge(who: object, confirm: bool) -> EmailMergeReport:
        calls.append((who, confirm))
        return EmailMergeReport(applied=confirm, examined=9, facts=2, superseded=3)

    available: Any = merge

    @asynccontextmanager
    async def fake_build(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        assert kwargs == {"storage": "postgres"}
        yield SimpleNamespace(principal=owner, email_memory_merge=available)

    monkeypatch.setattr(main, "build", fake_build)
    identity = f"{owner.tenant_id}/{owner.principal_id}"
    with pytest.raises(AuthorizationError):
        await main._memory_merge_email_duplicates("other/owner", True)
    preview = await main._memory_merge_email_duplicates(identity, False)
    assert preview["applied"] is False and preview["superseded"] == 3
    await main._memory_merge_email_duplicates(identity, True)
    assert calls == [(owner, False), (owner, True)]
    available = None
    with pytest.raises(NotFoundError, match="disabled"):
        await main._memory_merge_email_duplicates(identity, False)

    from typer.testing import CliRunner

    result = CliRunner().invoke(main.app, ["memory", "merge-email-duplicates", "--help"])
    assert result.exit_code == 0 and "--confirm" in result.output
