"""ADR-0341: one fact in an email thread forms one memory, however often it is assessed or quoted.

Production on 2026-11-20 held 668 live email memories, and 271 of them repeated
a value another memory from the same thread already stated: 157 from the same
message assessed again in different words, the rest from other messages.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_core.domain.email import EmailMessage
from agent_core.domain.email_memory import EmailMergeReport, quoted_text, stated_value
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


async def _copy(
    factory: Any, source: Any, original: MemoryRecord, number: int, **update: object
) -> MemoryRecord:
    """A copy as production holds them: another row for a fact, listed by its source message."""
    async with factory() as uow:
        copy: MemoryRecord = await uow.memories.upsert_belief(
            original.model_copy(
                update={
                    "id": UUID(int=9_000 + number),
                    "statement": original.statement.replace("moved to:", f"wording_{number}:"),
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
    assert rows[kept.id] == kept
    for copy in (same_message, later):
        merged = rows[copy.id]
        assert merged.status is MemoryStatus.SUPERSEDED and merged.superseded_by == kept.id
        assert merged.valid_to is not None and merged.store_position > copy.store_position
    # A copy under another subject, the owner's own memory and another thread are left alone.
    assert rows[other_subject.id] == other_subject and rows[owned.id] == owned
    assert rows[other_thread.id] == other_thread
    [audit] = [event for event in events if event.event_type == "email.memory.merged"]
    assert audit.payload == {
        "kept": str(kept.id),
        "superseded": sorted([str(same_message.id), str(later.id)]),
    }
    again = await service.merge_duplicates(apply=True)
    assert (again.facts, again.superseded) == (0, 0)
    # A superseded copy's message does not form the fact again, in any wording.
    result = await service.form(later_mail, [fact_for(later_mail, predicate="as_alex_said")])
    assert [row.id for row in result] == [later.id]
    assert len(await _email_memories(factory)) == 4


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
