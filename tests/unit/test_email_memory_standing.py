"""ADR-0340: email memories are kept by importance; known senders form first-class memories."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.domain.email import EmailFeedback, EmailMessage, EmailRecord, EmailThread
from agent_core.domain.email_memory import (
    EMAIL_RETENTION_POLICY,
    EmailMemoryStanding,
    thread_importance,
)
from agent_core.domain.email_semantics import EmailSemanticFact, thread_source_key
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryDerivation,
    MemoryLongevity,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    Sensitivity,
    decay_eligible_lifecycle,
    review_reason,
)
from agent_core.domain.policies import TrustLevel
from agent_core.memory.email_people import EmailPeopleFormationService
from agent_core.memory.formation import GovernedMemoryService
from tests.contract.memory_fixtures import user_event
from tests.contract.people_mail_fixtures import (
    ALEX,
    OWNER,
    correspondence_stack,
    mail,
    seed_account,
)
from tests.contract.support import NOW, SESSION_ID, principal

THREAD_ID = UUID(int=900)
BODY = "The Atlas board vote moved to Friday."
# The same fact in each sender's own words: a word-for-word repeat of an earlier
# message's sentence is quoted text and renews nothing (ADR-0341).
CONFIRMING = "Confirming the Atlas vote is now Friday."
AGREEING = "Yes, Friday is right for the Atlas vote."
SAM = "Sam <sam@example.test>"


async def _put(factory: Any, kind: str, key: str, payload: dict[str, object]) -> None:
    async with factory() as uow:
        current = await uow.email.get(principal(), kind, key)
        await uow.email.put(
            EmailRecord(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kind=kind,
                key=key,
                revision=1 if current is None else current.revision + 1,
                payload=payload,
                created_at=NOW if current is None else current.created_at,
                updated_at=NOW,
            ),
            expected_revision=0 if current is None else current.revision,
        )


def _message(source: Any, *, direction: str = "received", check: str = "pass") -> EmailMessage:
    """The message as the mailbox projection holds it, body included.

    Gmail's sender check passed unless a test says otherwise: a familiar From
    address counts for nothing without it.
    """
    return EmailMessage(
        id=source.message_id,
        sender=source.sender,
        body=source.body,
        sent_at=source.sent_at,
        direction=direction,  # type: ignore[arg-type]
        sender_check=check,  # type: ignore[arg-type]
    )


async def seed_thread(
    factory: Any,
    *,
    priority: float,
    messages: list[EmailMessage],
    provider_thread_id: str = "t-mail",
    thread_id: UUID = THREAD_ID,
    assessed: bool = True,
    complete: bool = True,
    fingerprint: str = "",
) -> EmailThread:
    """The thread as the mailbox projection stores it, with its assessed importance.

    A new message resets the stored rating to zero until the thread is assessed
    again (`assessed=False`), and a long thread keeps only some of its messages
    (`complete=False`). `fingerprint` names the revision of its messages, which
    a saved assessment records as the source it read.
    """
    thread = EmailThread(
        id=thread_id,
        account_id="work",
        provider_thread_id=provider_thread_id,
        subject="Atlas board",
        senders=sorted({message.sender for message in messages}),
        updated_at=NOW,
        last_accessed_at=NOW,
        priority=priority,
        messages=messages,
        assessment_version="email-experience@1" if assessed else "",
        complete=complete,
        source_fingerprint=fingerprint,
    )
    await _put(factory, "thread", str(thread.id), thread.model_dump(mode="json"))
    index_key = thread_source_key("work", provider_thread_id)
    async with factory() as uow:
        index = await uow.email.get(principal(), "thread_source", index_key)
    observed = set() if index is None else set(index.payload["observed_message_ids"])
    await _put(
        factory,
        "thread_source",
        index_key,
        {
            "thread_id": str(thread.id),
            # As the mailbox does: every message ever observed, whatever is stored now.
            "observed_message_ids": sorted(observed | {message.id for message in messages}),
        },
    )
    return thread


async def seed_feedback(factory: Any, judgment: str, *, undone: bool = False) -> None:
    item = EmailFeedback(
        id=UUID(int=901),
        thread_id=THREAD_ID,
        target="thread",
        judgment=judgment,  # type: ignore[arg-type]
        target_values=[str(THREAD_ID)],
        created_at=NOW,
        undone_at=NOW if undone else None,
    )
    await _put(factory, "feedback", str(item.id), item.model_dump(mode="json"))


def fact_for(
    source: Any,
    *,
    subject: str = "Atlas board vote",
    predicate: str = "moved to",
    value: str = "Friday",
    confidence: float = 0.9,
) -> EmailSemanticFact:
    return EmailSemanticFact(
        message_id=source.message_id,
        quote=source.body,
        belief_type=BeliefType.FACT,
        subject=subject,
        predicate=predicate,
        value=value,
        confidence=confidence,
    )


async def know_alex(factory: Any, service: EmailPeopleFormationService) -> None:
    """ADR-0121: the owner wrote to Alex, so Alex is in People."""
    await seed_account(factory)
    await service.register_source(
        await mail(
            factory, message_id="m-sent", sender=OWNER, to=ALEX, labels=["SENT"], days_ago=20
        )
    )


def _chat_memory(factory: Any, service: EmailPeopleFormationService) -> GovernedMemoryService:
    """The owner's own memory service, under the ordinary chat formation policy."""
    return GovernedMemoryService(
        factory,
        service._clock,
        SequenceIdFactory(UUID(int=value) for value in range(7_000, 7_100)),
        principal(),
    )


async def stored(factory: Any, memory: MemoryRecord) -> MemoryRecord:
    async with factory() as uow:
        current: MemoryRecord = await uow.memories.get(memory.id, principal())
    return current


@pytest.mark.parametrize(
    "importance, marked, replied, days",
    [
        (0.0, False, False, 30),
        (0.29, False, False, 30),
        (0.3, False, False, 90),
        (0.59, False, False, 90),
        (0.6, False, False, 365),
        (0.84, False, False, 365),
        (0.85, False, False, None),
        (1.0, False, False, None),
        (0.1, True, False, None),
        (0.1, False, True, None),
    ],
)
def test_retention_grows_with_the_threads_importance(
    importance: float, marked: bool, replied: bool, days: int | None
) -> None:
    standing = EmailMemoryStanding(
        importance=importance,
        owner_marked_important=marked,
        owner_replied=replied,
        known_sender=False,
    )
    assert standing.retention_days == days
    evidence = NOW - timedelta(days=3)
    assert standing.expires_at(evidence) == (
        None if days is None else evidence + timedelta(days=days)
    )
    # A stranger's claim is never first-class, however the thread is rated.
    assert not standing.first_class


@pytest.mark.parametrize(
    "importance, replied, first_class",
    [(0.59, False, False), (0.6, False, True), (0.1, True, True), (0.9, False, True)],
)
def test_first_class_needs_a_known_sender_and_a_thread_kept_a_year_or_more(
    importance: float, replied: bool, first_class: bool
) -> None:
    standing = EmailMemoryStanding(
        importance=importance,
        owner_marked_important=False,
        owner_replied=replied,
        known_sender=True,
    )
    assert standing.first_class is first_class


def test_importance_is_the_saved_rating_after_the_owners_importance_feedback() -> None:
    thread = EmailThread(
        id=THREAD_ID,
        account_id="work",
        provider_thread_id="t-mail",
        subject="Atlas board",
        updated_at=NOW,
        last_accessed_at=NOW,
        priority=0.4,
        # A finished dated event leaves the Inbox; its memory keeps its rating.
        attention_expires_at=NOW - timedelta(days=1),
    )

    def feedback(judgment: str, **updates: object) -> EmailFeedback:
        return EmailFeedback(
            id=UUID(int=902),
            thread_id=THREAD_ID,
            target="thread",
            judgment=judgment,  # type: ignore[arg-type]
            target_values=[str(THREAD_ID)],
            created_at=NOW,
        ).model_copy(update=updates)

    # The rating, whether the owner marked the thread Important, and whether it is known.
    assert thread_importance(thread, []) == (0.4, False, True)
    # A new message resets the stored rating; the last assessment still says what it was.
    reset = thread.model_copy(
        update={"priority": 0.0, "assessment_version": "", "source_fingerprint": "rev-2"}
    )
    assert thread_importance(reset, []) == (None, False, False)
    current = {"priority": 0.7, "source_fingerprint": "rev-2"}
    assert thread_importance(reset, [], current) == (0.7, False, True)
    # It rated the messages it read. Against another revision of the thread, or with
    # no revision recorded on either side, its rating is a floor and not a known rating.
    assert thread_importance(reset, [], {**current, "source_fingerprint": "rev-1"}) == (
        0.7,
        False,
        False,
    )
    assert thread_importance(reset, [], {"priority": 0.7}) == (0.7, False, False)
    unversioned = reset.model_copy(update={"source_fingerprint": ""})
    assert thread_importance(unversioned, [], {"priority": 0.7, "source_fingerprint": ""}) == (
        0.7,
        False,
        False,
    )
    # A record saved before ratings were kept gives a floor, which is not a known rating.
    older_record = {"content_importance": 0.95, "relationship_importance": 0.9, "urgency": 0.6}
    floor, _, known = thread_importance(reset, [], older_record)
    bulk_floor, _, _ = thread_importance(reset, [], {**older_record, "bulk": True})
    assert floor == pytest.approx(0.915) and bulk_floor == pytest.approx(0.366) and not known
    # Neither is the rating an assessment saved when it abstained or only screened for junk.
    ungrounded, grounded = {"priority": 0.4, "grounded": False}, {"priority": 0.4, "grounded": True}
    assert thread_importance(thread, [], ungrounded) == (0.4, False, False)
    assert thread_importance(thread, [], grounded) == (0.4, False, True)
    assert thread_importance(reset, [], {**ungrounded, "priority": 0.7}) == (0.7, False, False)
    # The owner's own judgment is always known.
    assert thread_importance(reset, [feedback("important")]) == (1.0, True, True)
    abstained = {"priority": 0.0, "grounded": False}
    assert thread_importance(thread, [feedback("less_important")], abstained) == (0.0, False, True)
    # A rating that survived with its version cleared (an expired body cache) is still the rating.
    kept = thread.model_copy(update={"assessment_version": ""})
    assert thread_importance(kept, [], {"priority": 0.9}) == (0.4, False, True)
    assert thread_importance(thread, [feedback("important")]) == (1.0, True, True)
    assert thread_importance(thread, [feedback("less_important")]) == (0.0, False, True)
    assert thread_importance(thread, [feedback("important", undone_at=NOW)]) == (0.4, False, True)
    assert thread_importance(thread, [feedback("needs_reply")]) == (0.4, False, True)


async def test_unknown_sender_in_an_important_thread_is_kept_longer_and_stays_a_guess() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.7, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.expires_at == source.sent_at + timedelta(days=365)
    assert memory.lifecycle_policy_version == EMAIL_RETENTION_POLICY
    assert memory.statement.startswith("Email from Alex Rivera")
    assert "mailbox work, thread t-mail, message m-in" in memory.statement
    assert memory.confidence <= 0.4
    assert memory.derivation is MemoryDerivation.HYPOTHESIS
    assert memory.longevity is MemoryLongevity.TENTATIVE
    assert memory.flagged_for_review and review_reason(memory) == "sensitive_automatic"
    assert memory.status is MemoryStatus.PROVISIONAL


async def test_known_sender_in_an_important_thread_forms_a_first_class_memory() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.7, messages=[_message(source, check="pass")])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.statement == (
        f"{ALEX} said by email on {source.sent_at.date().isoformat()} "
        'that Atlas board vote moved to: "Friday".'
    )
    # The extraction's confidence, up to the ceiling of an automatic chat capture.
    assert memory.confidence == 0.65
    assert memory.derivation is MemoryDerivation.DIRECT
    assert memory.longevity is MemoryLongevity.DURABLE
    assert not memory.flagged_for_review and review_reason(memory) is None
    assert memory.expires_at == source.sent_at + timedelta(days=365)
    assert memory.lifecycle_policy_version == EMAIL_RETENTION_POLICY
    # What does not change: attribution, authority, sensitivity, scope and status.
    assert memory.authority is MemoryAuthority.INFERRED
    assert memory.sensitivity is Sensitivity.SENSITIVE
    assert memory.portability is Portability.CONTEXTUAL
    assert memory.status is MemoryStatus.PROVISIONAL
    assert memory.valid_from == memory.last_evidence_at == source.sent_at
    assert memory.consolidation_policy_version == "email-semantic@2"
    assert await service.form(source, [fact_for(source)]) == [memory]


async def test_extraction_confidence_below_the_ceiling_is_kept_as_extracted() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.9, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source, confidence=0.5)])
    assert memory.confidence == 0.5 and memory.expires_at is None


@pytest.mark.parametrize(
    "priority, check, sender",
    [
        (0.2, "pass", ALEX),  # a known sender in an unimportant thread
        (0.9, "fail", ALEX),  # the sender check failed: the address proves nothing
        (0.9, "none", ALEX),  # no check passed: a From line alone is not a known sender
        (0.9, "none", OWNER),  # the owner's own address on received mail
    ],
)
async def test_every_other_email_memory_keeps_todays_construction(
    priority: float, check: str, sender: str
) -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=sender, body=BODY)
    await seed_thread(factory, priority=priority, messages=[_message(source, check=check)])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.statement.startswith("Email from ")
    assert memory.derivation is MemoryDerivation.HYPOTHESIS
    assert memory.confidence <= 0.4 and memory.flagged_for_review
    assert memory.expires_at == (source.sent_at + timedelta(days=30) if priority < 0.3 else None)


async def test_a_thread_with_no_stored_rating_keeps_the_thirty_day_guess() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.expires_at == source.sent_at + timedelta(days=30)
    assert memory.derivation is MemoryDerivation.HYPOTHESIS


async def test_the_owners_own_sent_mail_is_a_known_sender() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(
        factory, message_id="m-out", sender=OWNER, to=ALEX, labels=["SENT"], body=BODY
    )
    # Sent mail carries no Gmail verdict, and the owner's own needs none.
    sent = _message(source, direction="sent", check="none")
    await seed_thread(factory, priority=0.2, messages=[sent])
    [memory] = await service.form(source, [fact_for(source)])
    # The owner wrote in the thread, so it is kept without expiry and is first-class.
    assert memory.expires_at is None
    assert memory.statement.startswith(f"{OWNER} said by email on ")
    assert memory.authority is MemoryAuthority.INFERRED


async def test_memories_follow_a_later_change_in_their_threads_importance() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.2, messages=[_message(source)])
    [guess] = await service.form(source, [fact_for(source)])
    assert guess.expires_at == source.sent_at + timedelta(days=30)

    # The owner marks the thread Important: the same row becomes first-class.
    await seed_feedback(factory, "important")
    preview = await service.rerate_thread("work", "t-mail", apply=False)
    assert (preview.examined, preview.changed, preview.first_class) == (1, 1, 1)
    assert await stored(factory, guess) == guess
    report = await service.rerate_thread("work", "t-mail")
    assert (report.changed, report.promoted, report.by_retention) == (1, 1, {"no_expiry": 1})
    promoted = await stored(factory, guess)
    assert promoted.id == guess.id and promoted.expires_at is None
    assert promoted.statement.startswith(f"{ALEX} said by email on ")
    assert "mailbox" not in promoted.statement
    assert promoted.derivation is MemoryDerivation.DIRECT
    assert promoted.longevity is MemoryLongevity.DURABLE
    assert not promoted.flagged_for_review
    assert promoted.confidence == guess.confidence == 0.4
    assert promoted.store_position > guess.store_position
    assert promoted.last_evidence_at == guess.last_evidence_at == source.sent_at
    assert (await service.rerate_thread("work", "t-mail")).changed == 0

    # Undoing the mark returns it to a thirty-day guess with its record line.
    await seed_feedback(factory, "important", undone=True)
    report = await service.rerate_thread("work", "t-mail")
    assert (report.changed, report.demoted, report.by_retention) == (1, 1, {"30_days": 1})
    returned = await stored(factory, guess)
    assert returned.statement == guess.statement
    assert returned.derivation is MemoryDerivation.HYPOTHESIS
    assert returned.longevity is MemoryLongevity.TENTATIVE
    assert returned.flagged_for_review and returned.confidence <= 0.4
    assert returned.expires_at == source.sent_at + timedelta(days=30)


async def test_an_owner_reply_removes_the_expiry_when_the_sent_message_is_synced() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.2, messages=[_message(source)])
    [guess] = await service.form(source, [fact_for(source)])
    reply = await mail(
        factory, message_id="m-reply", sender=OWNER, to=ALEX, labels=["SENT"], days_ago=1
    )
    await service.register_source(reply)
    await seed_thread(
        factory, priority=0.2, messages=[_message(source), _message(reply, direction="sent")]
    )
    report = await service.rerate_thread("work", "t-mail")
    assert report.promoted == 1
    current = await stored(factory, guess)
    # ADR-0121: the reply is what makes Alex someone the owner knows.
    assert current.expires_at is None and current.derivation is MemoryDerivation.DIRECT


async def test_rerating_never_touches_a_memory_the_owner_confirmed() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.2, messages=[_message(source)])
    [guess] = await service.form(source, [fact_for(source)])
    async with factory() as uow:
        owned = await uow.memories.reinforce(
            guess.model_copy(
                update={"authority": MemoryAuthority.USER, "status": MemoryStatus.ACTIVE}
            )
        )
    await seed_feedback(factory, "important")
    report = await service.rerate_thread("work", "t-mail")
    assert (report.examined, report.changed) == (0, 0)
    assert await stored(factory, guess) == owned


async def test_rerating_every_source_previews_then_applies() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, thread_id="t-1")
    second = await mail(
        factory,
        message_id="m-2",
        sender="Sam <sam@example.test>",
        body="The diligence package is due Monday.",
        thread_id="t-2",
    )
    [one] = await service.form(first, [fact_for(first)])
    [two] = await service.form(
        second,
        [fact_for(second, subject="Diligence package", predicate="is due", value="Monday")],
    )
    await seed_thread(factory, priority=0.9, messages=[_message(first)], provider_thread_id="t-1")
    await seed_thread(
        factory,
        priority=0.4,
        messages=[_message(second)],
        provider_thread_id="t-2",
        thread_id=UUID(int=903),
    )
    preview = await service.rerate_all(apply=False)
    assert (preview.examined, preview.changed, preview.applied) == (2, 2, False)
    assert preview.by_retention == {"no_expiry": 1, "90_days": 1}
    assert (preview.first_class, preview.guesses) == (1, 1)
    assert await stored(factory, one) == one and await stored(factory, two) == two
    applied = await service.rerate_all(apply=True)
    assert applied.model_dump(exclude={"applied"}) == preview.model_dump(exclude={"applied"})
    assert (await stored(factory, one)).expires_at is None
    assert (await stored(factory, two)).expires_at == second.sent_at + timedelta(days=90)
    assert (await service.rerate_all(apply=True)).changed == 0


async def test_a_later_email_that_repeats_the_fact_renews_the_memory() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=40)
    later = await mail(factory, message_id="m-2", sender=ALEX, body=CONFIRMING, days_ago=2)
    echo = await mail(factory, message_id="m-3", sender=SAM, body=f"Noted.\n\n{BODY}", days_ago=1)
    await seed_thread(
        factory, priority=0.4, messages=[_message(first), _message(later), _message(echo)]
    )
    [memory] = await service.form(first, [fact_for(first)])
    assert memory.expires_at == first.sent_at + timedelta(days=90)

    [renewed] = await service.form(later, [fact_for(later)])
    assert renewed.id == memory.id
    assert renewed.last_evidence_at == later.sent_at
    assert renewed.expires_at == later.sent_at + timedelta(days=90)
    assert renewed.evidence_count == memory.evidence_count + 1
    assert renewed.valid_from == first.sent_at
    # A stranger cannot write a guess into the active set by sending it twice.
    assert renewed.status is MemoryStatus.PROVISIONAL
    assert renewed.derivation is MemoryDerivation.HYPOTHESIS
    # Reading either message again is the same evidence, not a new repeat.
    assert await service.form(later, [fact_for(later)]) == [renewed]
    assert await service.form(first, [fact_for(first)]) == [renewed]
    # A reply that carries the first message's sentence word for word renews nothing.
    quoted = fact_for(echo).model_copy(update={"quote": BODY})
    assert await service.form(echo, [quoted]) == []
    assert await stored(factory, memory) == renewed


async def test_older_mail_processed_later_renews_nothing() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    newer = await mail(factory, message_id="m-new", sender=ALEX, body=CONFIRMING, days_ago=2)
    older = await mail(factory, message_id="m-old", sender=ALEX, body=BODY, days_ago=60)
    await seed_thread(factory, priority=0.4, messages=[_message(older), _message(newer)])
    [memory] = await service.form(newer, [fact_for(newer)])
    # Import order is not evidence order: the older message arrives afterwards.
    [unchanged] = await service.form(older, [fact_for(older)])
    assert unchanged == memory
    assert unchanged.last_evidence_at == newer.sent_at
    assert unchanged.status is MemoryStatus.PROVISIONAL


async def test_a_correspondents_claim_never_replaces_what_the_owner_said() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    sequence = await user_event(factory, "The Atlas board vote is on Tuesday.")
    owner_belief, _ = await _chat_memory(factory, service).remember_formation(
        session_id=SESSION_ID,
        run_id=None,
        statement="The Atlas board vote is on Tuesday.",
        subject="Atlas board vote",
        scope="general",
        belief_type=BeliefType.FACT,
        portability=Portability.CONTEXTUAL,
        sensitivity=Sensitivity.INTERNAL,
        source_event_ids=[sequence],
        origin_trust=TrustLevel.USER,
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        polarity=Polarity.ASSERT,
        confidence=0.5,
        valid_from=NOW - timedelta(days=10),
        expires_at=None,
        trigger="test",
        record_audit=False,
        evidence_at=NOW - timedelta(days=10),
    )
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=3)
    await seed_thread(factory, priority=0.9, messages=[_message(source)])
    [claim] = await service.form(source, [fact_for(source)])
    kept = await stored(factory, owner_belief)
    assert kept.status is MemoryStatus.PROVISIONAL and kept.superseded_by is None
    assert kept.statement == "The Atlas board vote is on Tuesday."
    # Both stay, linked and flagged, so the owner decides which holds.
    assert claim.id != kept.id and claim.authority is MemoryAuthority.INFERRED
    assert kept.conflicts_with == [claim.id] and claim.conflicts_with == [kept.id]


async def test_email_memories_under_the_retention_table_are_outside_decay() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=80)
    await seed_thread(factory, priority=0.7, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source)])
    assert not decay_eligible_lifecycle(memory, 3)
    assert decay_eligible_lifecycle(
        memory.model_copy(update={"lifecycle_policy_version": "lifecycle@2"}), 3
    )
    async with factory() as uow:
        idle = await uow.memories.list_idle(
            principal(),
            evidence_before=NOW,
            decay_confidence_ceiling=0.55,
            min_unused_deliveries=3,
            limit=10,
        )
    assert memory.id not in {row.id for row in idle}
    result = await service._governed.decay(now=NOW + timedelta(days=200))
    assert (result.decayed, result.retired) == (0, 0)
    assert (await stored(factory, memory)).confidence == memory.confidence


async def test_a_first_class_claim_never_takes_over_a_hypothesis_formed_elsewhere() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    sequence = await user_event(factory, "The Atlas board vote might slip.")
    hypothesis, _ = await _chat_memory(factory, service).remember_formation(
        session_id=SESSION_ID,
        run_id=None,
        statement="The Atlas board vote may slip past this week.",
        subject="Atlas board vote",
        scope="general",
        belief_type=BeliefType.FACT,
        portability=Portability.CONTEXTUAL,
        sensitivity=Sensitivity.INTERNAL,
        source_event_ids=[sequence],
        origin_trust=TrustLevel.USER,
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        polarity=Polarity.ASSERT,
        confidence=0.3,
        valid_from=NOW - timedelta(days=10),
        expires_at=None,
        trigger="test",
        record_audit=False,
        derivation=MemoryDerivation.HYPOTHESIS,
        longevity=MemoryLongevity.TENTATIVE,
        evidence_at=NOW - timedelta(days=10),
    )
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=3)
    await seed_thread(factory, priority=0.9, messages=[_message(source)])
    [claim] = await service.form(source, [fact_for(source)])
    assert claim.id != hypothesis.id and claim.derivation is MemoryDerivation.DIRECT
    kept = await stored(factory, hypothesis)
    assert kept.statement == hypothesis.statement
    assert kept.derivation is MemoryDerivation.HYPOTHESIS
    assert kept.status is MemoryStatus.PROVISIONAL


async def test_a_first_class_memory_ranks_as_an_automatic_chat_capture_does() -> None:
    """No ranking code changes: the standing alone decides the recall treatment."""
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
    from agent_core.memory.retrieval import _asks_attention, _score
    from tests.contract.memory_fixtures import recall_query

    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.7, messages=[_message(source)])
    [first_class] = await service.form(source, [fact_for(source)])
    other = await mail(
        factory, message_id="m-2", sender="Sam <sam@example.test>", body=BODY, thread_id="t-2"
    )
    [guess] = await service.form(
        other, [fact_for(other, subject="Atlas board ballot", confidence=0.4)]
    )
    query = recall_query(text="Atlas board Friday", min_score=0)
    ranked, attributed = _score(first_class, query), _score(guess, query)
    assert ranked is not None and attributed is not None
    # The guess keeps the attention treatment of ADR-0249; the first-class memory has none.
    assert _asks_attention(attributed) and not _asks_attention(ranked)
    assert ranked.review_reason is None and attributed.review_reason == "sensitive_automatic"
    # A chat capture of the same confidence, status, authority and scope scores the same.
    chat_capture = first_class.model_copy(
        update={"consolidation_policy_version": "formation@11", "statement": first_class.statement}
    )
    assert _score(chat_capture, query) == ranked
    weights = DEFAULT_RETRIEVAL_PROFILE.ranking_weights
    lifecycle = DEFAULT_RETRIEVAL_PROFILE.lifecycle_weights.provisional
    assert ranked.score - attributed.score == pytest.approx(
        weights.confidence * lifecycle * (first_class.confidence - guess.confidence)
        + DEFAULT_RETRIEVAL_PROFILE.attention_penalty
    )
    # Attribution is always shown: the sender and the channel are in the line itself.
    assert "Alex Rivera" in ranked.statement and "said by email on" in ranked.statement
    assert ranked.authority is MemoryAuthority.INFERRED


async def test_a_memory_the_owners_own_words_took_over_is_no_longer_an_email_memory() -> None:
    """An owner statement can promote an email guess in place; the row keeps the email policy
    label but holds the owner's words, so neither re-rating nor a later email may touch it."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=9)
    await seed_thread(factory, priority=0.2, messages=[_message(source)])
    [guess] = await service.form(source, [fact_for(source)])
    async with factory() as uow:
        taken_over = await uow.memories.reinforce(
            guess.model_copy(
                update={
                    "statement": "The Atlas board vote is on Tuesday.",
                    "derivation": MemoryDerivation.DIRECT,
                    "longevity": MemoryLongevity.DURABLE,
                    "status": MemoryStatus.ACTIVE,
                    "expires_at": None,
                    "lifecycle_policy_version": "lifecycle@2",
                }
            )
        )
    assert (await service.rerate_thread("work", "t-mail")).examined == 0
    assert await stored(factory, guess) == taken_over

    later = await mail(
        factory, message_id="m-2", sender=ALEX, body="The Atlas board vote moved to Monday."
    )
    await seed_thread(factory, priority=0.9, messages=[_message(source), _message(later)])
    [claim] = await service.form(later, [fact_for(later, value="Monday")])
    kept = await stored(factory, guess)
    assert claim.id != kept.id and kept.status is MemoryStatus.ACTIVE
    assert kept.statement == "The Atlas board vote is on Tuesday." and kept.superseded_by is None


async def test_a_repeat_from_a_known_sender_makes_a_first_class_memory_active() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=12)
    stranger = await mail(factory, message_id="m-2", sender=SAM, body=CONFIRMING, days_ago=6)
    later = await mail(factory, message_id="m-3", sender=ALEX, body=AGREEING, days_ago=2)
    await seed_thread(
        factory,
        priority=0.7,
        messages=[_message(first), _message(stranger), _message(later)],
    )
    [memory] = await service.form(first, [fact_for(first)])
    assert memory.derivation is MemoryDerivation.DIRECT
    assert memory.status is MemoryStatus.PROVISIONAL

    # A stranger repeating it renews the dates and settles nothing.
    [echoed] = await service.form(stranger, [fact_for(stranger)])
    assert echoed.id == memory.id and echoed.last_evidence_at == stranger.sent_at
    assert echoed.status is MemoryStatus.PROVISIONAL

    # The same from someone the owner knows makes it active, as a repeat does in chat.
    [settled] = await service.form(later, [fact_for(later)])
    assert settled.id == memory.id and settled.status is MemoryStatus.ACTIVE
    assert settled.last_evidence_at == later.sent_at
    assert settled.expires_at == later.sent_at + timedelta(days=365)
    assert settled.statement == memory.statement and not settled.flagged_for_review


async def test_a_historical_import_keeps_the_thirty_day_horizon_from_the_source_date() -> None:
    """Gate 26: an import never makes an old claim current, however its thread is rated."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-old", sender=ALEX, body=BODY, days_ago=200)
    await seed_thread(factory, priority=0.95, messages=[_message(source)])
    await seed_feedback(factory, "important")

    async def guard(uow: Any) -> None:
        return None

    importer = EmailPeopleFormationService(
        factory,
        service._clock,
        SequenceIdFactory(UUID(int=value) for value in range(8_000, 8_100)),
        principal(),
        provider="fake",
        model="scripted",
        import_window=(source.sent_at - timedelta(days=1), source.sent_at + timedelta(days=1)),
        import_guard=guard,
    )
    [memory] = await importer.form(source, [fact_for(source)])
    assert memory.expires_at == source.sent_at + timedelta(days=30)
    assert memory.expires_at < NOW
    assert memory.derivation is MemoryDerivation.HYPOTHESIS
    assert memory.statement.startswith("Email from ")
    # Re-rating its thread afterwards does not revive it.
    assert (await service.rerate_thread("work", "t-mail")).examined == 0
    assert await stored(factory, memory) == memory


async def test_memories_formed_before_the_change_wait_for_the_owners_command() -> None:
    """Nothing is backfilled silently: the hooks follow only memories already under the table."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.9, messages=[_message(source)])
    [formed] = await service.form(source, [fact_for(source)])
    async with factory() as uow:
        # As production holds it today: a thirty-day flagged guess under the old label.
        earlier = await uow.memories.reinforce(
            formed.model_copy(
                update={
                    "statement": (
                        f"Email from {ALEX} on {source.sent_at.date().isoformat()} "
                        "(mailbox work, thread t-mail, message m-in) reports "
                        'Atlas board vote moved to: "Friday".'
                    ),
                    "derivation": MemoryDerivation.HYPOTHESIS,
                    "longevity": MemoryLongevity.TENTATIVE,
                    "confidence": 0.4,
                    "flagged_for_review": True,
                    "expires_at": source.sent_at + timedelta(days=30),
                    "lifecycle_policy_version": "lifecycle@2",
                }
            )
        )
    # An assessment or the owner's feedback on the thread leaves it as it was.
    report = await service.rerate_thread("work", "t-mail")
    assert (report.examined, report.changed) == (0, 0)
    assert await stored(factory, formed) == earlier

    preview = await service.rerate_all(apply=False)
    assert (preview.examined, preview.changed, preview.promoted) == (1, 1, 1)
    assert await stored(factory, formed) == earlier
    assert (await service.rerate_all(apply=True)).changed == 1
    current = await stored(factory, formed)
    assert current.lifecycle_policy_version == EMAIL_RETENTION_POLICY
    assert current.expires_at is None and current.derivation is MemoryDerivation.DIRECT
    # Once the command has brought it under the table, it follows its thread.
    await seed_feedback(factory, "less_important")
    assert (await service.rerate_thread("work", "t-mail")).demoted == 1


async def test_a_reset_rating_falls_back_to_the_threads_last_assessment() -> None:
    """A new message zeroes the stored rating until the next assessment; zero is not a rating."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.0, messages=[_message(source)], assessed=False)
    await _put(
        factory,
        "assessment",
        str(THREAD_ID),
        {"content_importance": 0.95, "relationship_importance": 0.9, "urgency": 0.6, "bulk": False},
    )
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.expires_at is None

    other = await mail(
        factory, message_id="m-2", sender=ALEX, body="The diligence package is due Monday."
    )
    await seed_thread(
        factory, priority=0.0, messages=[_message(source), _message(other)], assessed=False
    )
    await _put(factory, "assessment", str(THREAD_ID), {"priority": 0.7})
    [second] = await service.form(
        other, [fact_for(other, subject="Diligence package", predicate="is due", value="Monday")]
    )
    assert second.expires_at == other.sent_at + timedelta(days=365)


async def test_rerating_lowers_a_memory_only_on_a_current_complete_reading_of_its_thread() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    newer = await mail(factory, message_id="m-new", sender="Sam <sam@example.test>", days_ago=1)
    await seed_thread(factory, priority=0.7, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.derivation is MemoryDerivation.DIRECT
    assert memory.expires_at == source.sent_at + timedelta(days=365)

    # A new message arrives: the rating is reset and nothing has assessed the thread yet.
    await seed_thread(
        factory, priority=0.0, messages=[_message(source), _message(newer)], assessed=False
    )
    assert (await service.rerate_thread("work", "t-mail")).changed == 0
    # A long thread keeps a window of its messages; this memory's message scrolled out of it.
    await seed_thread(factory, priority=0.1, messages=[_message(newer)], complete=False)
    assert (await service.rerate_thread("work", "t-mail")).changed == 0
    # Rated low, but part of the thread is unread: an owner reply may be in the unread part.
    await seed_thread(
        factory, priority=0.1, messages=[_message(source), _message(newer)], complete=False
    )
    assert (await service.rerate_thread("work", "t-mail")).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0
    assert await stored(factory, memory) == memory

    # Raising needs no such care: the owner's mark holds whatever else is unknown.
    await seed_feedback(factory, "important")
    await seed_thread(factory, priority=0.0, messages=[_message(newer)], assessed=False)
    report = await service.rerate_thread("work", "t-mail")
    assert (report.changed, report.by_retention) == (1, {"no_expiry": 1})
    raised = await stored(factory, memory)
    assert raised.expires_at is None and raised.derivation is MemoryDerivation.DIRECT

    # A current, complete reading that rates the thread low does lower it.
    await seed_feedback(factory, "important", undone=True)
    await seed_thread(factory, priority=0.1, messages=[_message(source), _message(newer)])
    report = await service.rerate_thread("work", "t-mail")
    assert (report.changed, report.demoted, report.by_retention) == (1, 1, {"30_days": 1})
    lowered = await stored(factory, memory)
    assert lowered.derivation is MemoryDerivation.HYPOTHESIS
    assert lowered.expires_at == source.sent_at + timedelta(days=30)


def test_the_rating_formula_is_one_function_shared_with_the_assessment() -> None:
    from agent_core.domain.email import assessment_priority

    assert assessment_priority(0.6, 0.0, 0.0, bulk=False) == pytest.approx(0.54)
    assert assessment_priority(0.2, 0.9, 0.5, bulk=False) == pytest.approx(0.74)
    assert assessment_priority(1.0, 1.0, 1.0, bulk=False) == 1.0
    assert assessment_priority(0.5, 0.0, 0.0, bulk=True) == pytest.approx(0.18)


async def test_a_floor_from_an_older_assessment_record_never_shortens_a_memory() -> None:
    """An assessment saved before ratings were kept holds only the model's features. Their
    content share is a floor: content 0.4 with relationship 1.0 rated 0.82, the floor is 0.36."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.82, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.expires_at == source.sent_at + timedelta(days=365)

    # A new message resets the stored rating; only the older record answers for it.
    await seed_thread(
        factory, priority=0.0, messages=[_message(source)], assessed=False, fingerprint="rev-2"
    )
    features: dict[str, object] = {
        "content_importance": 0.4,
        "relationship_importance": 1.0,
        "urgency": 0,
    }
    await _put(factory, "assessment", str(THREAD_ID), features)
    assert (await service.rerate_thread("work", "t-mail")).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0
    assert await stored(factory, memory) == memory

    # The floor still lengthens: content 0.95 alone is enough for no expiry.
    await _put(factory, "assessment", str(THREAD_ID), {**features, "content_importance": 0.95})
    assert (await service.rerate_thread("work", "t-mail")).by_retention == {"no_expiry": 1}
    # The rating itself, kept with every assessment saved since, does lower, when the
    # assessment read the thread as it is stored now.
    rated = {**features, "priority": 0.36, "source_fingerprint": "rev-2"}
    await _put(factory, "assessment", str(THREAD_ID), rated)
    assert (await service.rerate_thread("work", "t-mail")).by_retention == {"90_days": 1}


async def test_an_assessment_that_abstained_shortens_and_demotes_nothing() -> None:
    """A rejected or ungrounded result is saved with zero features, and the thread's rating
    with them. That zero is no reading of the thread."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=40)
    await seed_thread(factory, priority=0.7, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.derivation is MemoryDerivation.DIRECT
    assert memory.expires_at == source.sent_at + timedelta(days=365)

    await seed_thread(factory, priority=0.0, messages=[_message(source)])
    abstained: dict[str, object] = {
        "priority": 0.0,
        "content_importance": 0,
        "urgency": 0,
        "grounded": False,
    }
    await _put(factory, "assessment", str(THREAD_ID), abstained)
    assert (await service.rerate_thread("work", "t-mail")).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0
    assert await stored(factory, memory) == memory

    # The owner's mark still lengthens it while the assessment abstains.
    await seed_feedback(factory, "important")
    assert (await service.rerate_thread("work", "t-mail")).by_retention == {"no_expiry": 1}
    await seed_feedback(factory, "important", undone=True)
    # A grounded assessment that rates the thread low is a reading, and lowers it.
    await _put(factory, "assessment", str(THREAD_ID), {**abstained, "grounded": True})
    report = await service.rerate_thread("work", "t-mail")
    assert (report.demoted, report.by_retention) == (1, {"30_days": 1})


async def test_same_dated_mail_from_a_stranger_never_restands_a_known_senders_memory() -> None:
    """A message governs the memories it formed or renewed, and is told apart by its own
    identity: two messages can carry the same timestamp."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=5)
    twin = await mail(factory, message_id="m-2", sender=SAM, body=CONFIRMING, days_ago=5)
    assert first.sent_at == twin.sent_at
    await seed_thread(factory, priority=0.7, messages=[_message(first), _message(twin)])
    [memory] = await service.form(first, [fact_for(first)])
    assert memory.derivation is MemoryDerivation.DIRECT
    # The same-dated message finds the memory and changes nothing.
    assert await service.form(twin, [fact_for(twin)]) == [memory]

    assert (await service.rerate_thread("work", "t-mail")).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0
    current = await stored(factory, memory)
    assert current == memory and "message m-2" not in current.statement


async def _without_roles(factory: Any, *sources: Any) -> None:
    """Leave these messages' records as one written before roles were kept: each lists
    its memories and says nothing of what the message did for them."""
    from agent_core.domain.email_semantics import semantic_source_key

    for source in sources:
        key = semantic_source_key(source.account_id, source.provider_thread_id, source.message_id)
        async with factory() as uow:
            record = await uow.email.get(principal(), "semantic_source", key)
        assert record is not None
        facts = {
            name: value
            for name, value in record.payload["facts"].items()
            if not name.startswith(("formed:", "renewed:", "seen:"))
        }
        await _put(factory, "semantic_source", key, {**record.payload, "facts": facts})


@pytest.mark.parametrize("stripped", ["both", "twin", "first"])
async def test_a_record_without_roles_never_restands_a_memory_it_is_not_named_by(
    stripped: str,
) -> None:
    """Two messages with one sent time both list the memory. Without a role, a record is
    told apart by the memory's own record line; a plain statement names no message, so
    neither record may restate it, demote it or rewrite who said it."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=5)
    twin = await mail(
        factory, message_id="m-2", sender=SAM, body=CONFIRMING, days_ago=5, thread_id="t-2"
    )
    assert first.sent_at == twin.sent_at
    await seed_thread(factory, priority=0.7, messages=[_message(first)])
    # The twin sits in a thread a complete reading rates low, from a sender nobody knows.
    await seed_thread(
        factory,
        priority=0.1,
        messages=[_message(twin)],
        provider_thread_id="t-2",
        thread_id=UUID(int=906),
    )
    [memory] = await service.form(first, [fact_for(first)])
    assert memory.derivation is MemoryDerivation.DIRECT
    assert await service.form(twin, [fact_for(twin)]) == [memory]
    await _without_roles(
        factory, *{"both": (first, twin), "twin": (twin,), "first": (first,)}[stripped]
    )

    for thread in ("t-mail", "t-2"):
        assert (await service.rerate_thread("work", thread)).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0
    current = await stored(factory, memory)
    assert current == memory and "message m-2" not in current.statement


async def test_a_record_without_roles_restands_the_memory_its_own_record_line_names() -> None:
    """Every memory formed before roles were kept is a record line that names its message,
    so the owner's command still promotes it from that message and from no other."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=5)
    twin = await mail(factory, message_id="m-2", sender=SAM, body=CONFIRMING, days_ago=5)
    await seed_thread(factory, priority=0.2, messages=[_message(first), _message(twin)])
    [guess] = await service.form(first, [fact_for(first)])
    assert guess.derivation is MemoryDerivation.HYPOTHESIS and "message m-1" in guess.statement
    assert await service.form(twin, [fact_for(twin)]) == [guess]
    await _without_roles(factory, first, twin)

    await seed_feedback(factory, "important")
    report = await service.rerate_all(apply=True)
    assert (report.promoted, report.demoted, report.by_retention) == (1, 0, {"no_expiry": 1})
    promoted = await stored(factory, guess)
    assert promoted.statement.startswith(f"{ALEX} said by email on ")
    assert promoted.derivation is MemoryDerivation.DIRECT
    # Promoted, its statement names no message any more. The record line named the
    # forming message while it stood, and that was written down: the memory still
    # follows that message, and undoing the mark returns it to the same guess.
    await seed_feedback(factory, "important", undone=True)
    report = await service.rerate_all(apply=True)
    assert (report.demoted, report.by_retention) == (1, {"30_days": 1})
    assert (await stored(factory, guess)).statement == guess.statement


@pytest.mark.parametrize("swept", [False, True])
async def test_undoing_feedback_restores_a_memory_the_feedback_itself_left_past_due(
    swept: bool,
) -> None:
    """Less important on a forty-day-old memory of a thread kept a year sets its expiry
    ten days in the past. Undoing it puts the memory back, also once the sweep closed it."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=40)
    await seed_thread(factory, priority=0.7, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.derivation is MemoryDerivation.DIRECT
    assert memory.expires_at == source.sent_at + timedelta(days=365)

    await seed_feedback(factory, "less_important")
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert (report.demoted, report.by_retention) == (1, {"30_days": 1})
    lapsed = await stored(factory, memory)
    assert lapsed.expires_at == source.sent_at + timedelta(days=30) and lapsed.expires_at < NOW
    if swept:
        [closed] = await service._governed.expire()
        assert closed.id == memory.id and closed.status is MemoryStatus.EXPIRED

    await seed_feedback(factory, "less_important", undone=True)
    preview = await service.rerate_thread("work", "t-mail", apply=False, cause="feedback")
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert (report.changed, report.promoted, report.by_retention) == (1, 1, {"1_year": 1})
    assert preview.model_dump(exclude={"applied"}) == report.model_dump(exclude={"applied"})
    restored = await stored(factory, memory)
    assert restored.status is memory.status and restored.valid_to is None
    assert restored.expires_at == memory.expires_at
    assert restored.statement == memory.statement
    assert restored.derivation is MemoryDerivation.DIRECT
    # The note is spent with the restoring: nothing is left to do.
    assert (await service.rerate_thread("work", "t-mail", cause="feedback")).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0


async def test_a_memory_that_expired_on_its_own_is_never_revived() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    # Formed from old mail in a thread kept thirty days: past its expiry from the start.
    old = await mail(factory, message_id="m-old", sender=ALEX, body=BODY, days_ago=40)
    await seed_thread(factory, priority=0.2, messages=[_message(old)])
    [expired] = await service.form(old, [fact_for(old)])
    assert expired.expires_at is not None and expired.expires_at < NOW
    # Lowered past due by a grounded assessment of its thread, not by the owner.
    rated = await mail(
        factory,
        message_id="m-2",
        sender=ALEX,
        body="The diligence package is due Monday.",
        days_ago=40,
        thread_id="t-2",
    )
    await seed_thread(
        factory,
        priority=0.7,
        messages=[_message(rated)],
        provider_thread_id="t-2",
        thread_id=UUID(int=907),
    )
    [lowered] = await service.form(
        rated, [fact_for(rated, subject="Diligence package", predicate="is due", value="Monday")]
    )
    await seed_thread(
        factory,
        priority=0.1,
        messages=[_message(rated)],
        provider_thread_id="t-2",
        thread_id=UUID(int=907),
    )
    assert (await service.rerate_thread("work", "t-2")).by_retention == {"30_days": 1}
    lowered = await stored(factory, lowered)

    # The owner now marks the sender Important: neither memory comes back.
    item = EmailFeedback(
        id=UUID(int=908),
        thread_id=THREAD_ID,
        target="person",
        judgment="important",
        target_values=["alex@example.test"],
        created_at=NOW,
    )
    await _put(factory, "feedback", str(item.id), item.model_dump(mode="json"))
    for thread in ("t-mail", "t-2"):
        report = await service.rerate_thread("work", thread, cause="feedback")
        assert (report.examined, report.changed) == (0, 0)
    assert (await service.lengthen_threads([("work", "t-mail"), ("work", "t-2")])).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0
    assert await stored(factory, expired) == expired
    assert await stored(factory, lowered) == lowered


@pytest.mark.parametrize("paused", [False, True])
async def test_a_memory_an_undone_mark_left_past_due_returns_when_the_owner_marks_it_again(
    paused: bool,
) -> None:
    """The same gap from the other side: undoing Important returns a memory to a
    thirty-day tier it has already outlived. Marking the thread again puts it back,
    also when every one of these marks is given while learning is paused."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=20)
    await seed_thread(factory, priority=0.2, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source)])
    await _pause(factory, paused)
    await seed_feedback(factory, "important")
    assert (await service.rerate_thread("work", "t-mail", cause="feedback")).changed == 1
    service._clock.advance(timedelta(days=15))  # type: ignore[attr-defined]

    await seed_feedback(factory, "important", undone=True)
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert report.by_retention == {"30_days": 1}
    await service._governed.expire()
    assert (await stored(factory, memory)).status is MemoryStatus.EXPIRED

    await seed_feedback(factory, "important")
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert (report.changed, report.by_retention) == (1, {"no_expiry": 1})
    restored = await stored(factory, memory)
    assert restored.status is memory.status and restored.expires_at is None
    assert restored.valid_to is None


async def test_a_memory_the_owners_less_important_lowered_at_an_assessment_returns_on_undo() -> (
    None
):
    """The same gap on the other path: Less important on a person reaches another thread
    at its next assessment. Undone, the pass over that thread puts its memory back."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(
        factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=40, thread_id="t-2"
    )
    await seed_thread(factory, priority=0.7, messages=[_message(source)], provider_thread_id="t-2")
    [memory] = await service.form(source, [fact_for(source)])

    async def person_feedback(*, undone: bool) -> None:
        item = EmailFeedback(
            id=UUID(int=909),
            thread_id=UUID(int=904),  # given on another of Alex's threads
            target="person",
            judgment="less_important",
            target_values=["alex@example.test"],
            created_at=NOW,
            undone_at=NOW if undone else None,
        )
        await _put(factory, "feedback", str(item.id), item.model_dump(mode="json"))

    await person_feedback(undone=False)
    # The pass over other threads shortens nothing; the thread's own assessment does.
    assert (await service.lengthen_threads([("work", "t-2")])).changed == 0
    assert (await service.rerate_thread("work", "t-2")).by_retention == {"30_days": 1}
    await service._governed.expire()
    assert (await stored(factory, memory)).status is MemoryStatus.EXPIRED

    await person_feedback(undone=True)
    report = await service.lengthen_threads([("work", "t-2")])
    assert (report.changed, report.by_retention) == (1, {"1_year": 1})
    restored = await stored(factory, memory)
    assert restored.status is memory.status and restored.expires_at == memory.expires_at


async def test_a_preview_drops_a_projection_once_the_stored_memory_has_changed() -> None:
    """A preview carries a memory between two visits as the first would have left it.
    If the owner takes the memory over in between, the second visit reads the store."""
    from agent_core.domain.email_semantics import semantic_source_key
    from agent_core.memory.email_people import _Rerating

    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=40)
    later = await mail(factory, message_id="m-2", sender=ALEX, body=CONFIRMING, days_ago=2)
    await seed_thread(factory, priority=0.4, messages=[_message(first), _message(later)])
    [guess] = await service.form(first, [fact_for(first)])
    [renewed] = await service.form(later, [fact_for(later)])
    await seed_feedback(factory, "important")

    async def visit(source: Any, seen: _Rerating) -> list[UUID]:
        key = semantic_source_key(source.account_id, source.provider_thread_id, source.message_id)
        async with factory() as uow:
            record = await uow.email.get(principal(), "semantic_source", key)
            assert record is not None
            return await service._rerate_source(uow, record, seen, apply=False, adopt=True)

    # Unchanged in between, the second visit builds on the first: one promoted memory.
    seen = _Rerating()
    assert await visit(first, seen) == [guess.id] and await visit(later, seen) == [guess.id]
    assert seen.report(applied=False).first_class == 1

    seen = _Rerating()
    assert await visit(first, seen) == [guess.id]
    async with factory() as uow:
        owned = await uow.memories.reinforce(
            renewed.model_copy(
                update={"authority": MemoryAuthority.USER, "status": MemoryStatus.ACTIVE}
            )
        )
    # The owner's memory is no longer the table's: the later visit leaves it alone.
    assert await visit(later, seen) == []
    assert seen.retention == {} and await stored(factory, guess) == owned


async def test_a_preview_reports_the_standing_the_apply_will_leave() -> None:
    """The message that formed a renewed memory sets its standing and the later one its
    retention; a preview carries the first visit's outcome into the second, as a write does."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=40)
    later = await mail(factory, message_id="m-2", sender=ALEX, body=CONFIRMING, days_ago=2)
    await seed_thread(factory, priority=0.4, messages=[_message(first), _message(later)])
    [guess] = await service.form(first, [fact_for(first)])
    [renewed] = await service.form(later, [fact_for(later)])
    assert renewed.id == guess.id and renewed.derivation is MemoryDerivation.HYPOTHESIS

    await seed_feedback(factory, "important")
    preview = await service.rerate_thread("work", "t-mail", apply=False)
    assert await stored(factory, guess) == renewed
    applied = await service.rerate_thread("work", "t-mail")
    assert (applied.promoted, applied.first_class, applied.guesses) == (1, 1, 0)
    assert preview.model_dump(exclude={"applied"}) == applied.model_dump(exclude={"applied"})


async def test_the_owners_mark_between_two_pages_of_a_rerating_is_not_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command commits a page of sources at a time. What it read for one page says
    nothing about the next: the owner may have marked the thread Important in between."""
    from contextlib import asynccontextmanager

    factory, service = await correspondence_stack()
    await seed_account(factory)
    one = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=4)
    two = await mail(
        factory, message_id="m-2", sender=ALEX, body="The diligence package is due Monday."
    )
    await seed_thread(factory, priority=0.2, messages=[_message(one), _message(two)])
    [first] = await service.form(one, [fact_for(one)])
    [second] = await service.form(
        two, [fact_for(two, subject="Diligence package", predicate="is due", value="Monday")]
    )
    monkeypatch.setattr("agent_core.memory.email_people._RERATE_PAGE", 1)
    hook = EmailPeopleFormationService(
        factory, service._clock, service._ids, principal(), provider="fake", model="scripted"
    )
    transactions = 0

    @asynccontextmanager
    async def paged() -> Any:
        nonlocal transactions
        transactions += 1
        if transactions == 2:
            # Between the first page and the second: the owner's mark, and its hook.
            await seed_feedback(factory, "important")
            await hook.rerate_thread("work", "t-mail", cause="feedback")
        async with factory() as uow:
            yield uow

    monkeypatch.setattr(service, "_uow_factory", paged)
    report = await service.rerate_all(apply=True)
    assert transactions == 3
    # The second page was read and its memory examined, not merely stepped over. The
    # report holds what each page found: thirty days before the mark, no expiry after.
    assert (report.examined, report.changed) == (2, 0)
    assert report.by_retention == {"30_days": 1, "no_expiry": 1}
    assert (await stored(factory, first)).expires_at is None
    assert (await stored(factory, second)).expires_at is None


async def test_a_first_class_claim_never_upgrades_a_hypothesis_it_repeats() -> None:
    """Only the owner's own statement promotes a guess, also when the claim is a duplicate."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=3)
    statement = (
        f"{ALEX} said by email on {source.sent_at.date().isoformat()} "
        'that Atlas board vote moved to: "Friday".'
    )
    sequence = await user_event(factory, "Alex emailed about the Atlas board vote.")
    hypothesis, _ = await _chat_memory(factory, service).remember_formation(
        session_id=SESSION_ID,
        run_id=None,
        statement=statement,
        subject="Atlas board vote",
        scope="general",
        belief_type=BeliefType.FACT,
        portability=Portability.CONTEXTUAL,
        sensitivity=Sensitivity.INTERNAL,
        source_event_ids=[sequence],
        origin_trust=TrustLevel.USER,
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        polarity=Polarity.ASSERT,
        confidence=0.3,
        valid_from=NOW - timedelta(days=10),
        expires_at=None,
        trigger="test",
        record_audit=False,
        derivation=MemoryDerivation.HYPOTHESIS,
        longevity=MemoryLongevity.TENTATIVE,
        evidence_at=NOW - timedelta(days=10),
    )
    await seed_thread(factory, priority=0.9, messages=[_message(source)])
    [formed] = await service.form(source, [fact_for(source)])
    kept = await stored(factory, hypothesis)
    # The repeat is evidence for the guess that was stored, and forms no second memory.
    assert formed.id == hypothesis.id
    assert kept.evidence_count == hypothesis.evidence_count + 1
    assert kept.derivation is MemoryDerivation.HYPOTHESIS
    assert kept.longevity is MemoryLongevity.TENTATIVE


async def test_the_pass_over_a_persons_other_threads_lengthens_and_promotes_and_never_lowers() -> (
    None
):
    """A mark on a person rates every thread from them. Their other threads' memories
    gain what the mark gives at once; what it would take away waits for a full re-rating."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, thread_id="t-2")
    await seed_thread(factory, priority=0.2, messages=[_message(source)], provider_thread_id="t-2")
    [guess] = await service.form(source, [fact_for(source)])
    assert guess.expires_at == source.sent_at + timedelta(days=30)

    async def person_feedback(judgment: str, *, undone: bool = False) -> None:
        item = EmailFeedback(
            id=UUID(int=905),
            thread_id=UUID(int=904),  # given on another of Alex's threads
            target="person",
            judgment=judgment,  # type: ignore[arg-type]
            target_values=["alex@example.test"],
            created_at=NOW,
            undone_at=NOW if undone else None,
        )
        await _put(factory, "feedback", str(item.id), item.model_dump(mode="json"))

    # Less important on the person would lower this memory; the pass leaves it alone.
    await seed_thread(factory, priority=0.7, messages=[_message(source)], provider_thread_id="t-2")
    assert (await service.rerate_thread("work", "t-2")).promoted == 1
    first_class = await stored(factory, guess)
    await person_feedback("less_important")
    assert (await service.lengthen_threads([("work", "t-2")])).changed == 0
    assert await stored(factory, guess) == first_class

    # Important on the person lengthens it at once.
    await person_feedback("important")
    report = await service.lengthen_threads([("work", "t-2"), ("work", "t-unknown")])
    assert (report.changed, report.demoted, report.by_retention) == (1, 0, {"no_expiry": 1})
    raised = await stored(factory, guess)
    assert raised.expires_at is None and raised.derivation is MemoryDerivation.DIRECT
    async with factory() as uow:
        events = await uow.events.list_after(guess.source_session_id, 0, principal())
    assert [e.payload["cause"] for e in events if e.event_type == "email.memory.rerated"] == [
        "assessment",
        "feedback",
    ]

    # Undone, the pass takes nothing back; the thread's own re-rating does.
    await person_feedback("important", undone=True)
    await seed_thread(factory, priority=0.2, messages=[_message(source)], provider_thread_id="t-2")
    assert (await service.lengthen_threads([("work", "t-2")])).changed == 0
    assert await stored(factory, guess) == raised
    report = await service.rerate_thread("work", "t-2")
    assert (report.demoted, report.by_retention) == (1, {"30_days": 1})


async def test_the_pass_over_other_threads_promotes_a_guess_the_mark_makes_first_class() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, thread_id="t-2")
    await seed_thread(factory, priority=0.2, messages=[_message(source)], provider_thread_id="t-2")
    [guess] = await service.form(source, [fact_for(source)])
    item = EmailFeedback(
        id=UUID(int=905),
        thread_id=UUID(int=904),
        target="person",
        judgment="important",
        target_values=["alex@example.test"],
        created_at=NOW,
    )
    await _put(factory, "feedback", str(item.id), item.model_dump(mode="json"))
    # The pass runs inside the owner's request. Once its time is spent it starts no
    # new thread, and the thread waits for its own assessment or the command.
    readings = iter([0.0, 5.0, 5.0])
    spent = await service.lengthen_threads([("work", "t-2")], elapsed=lambda: next(readings))
    assert spent.examined == 0
    assert await stored(factory, guess) == guess

    # Within its time the same pass reaches the thread.
    report = await service.lengthen_threads([("work", "t-2")], elapsed=lambda: 0.0)
    assert (report.promoted, report.by_retention) == (1, {"no_expiry": 1})
    promoted = await stored(factory, guess)
    assert promoted.derivation is MemoryDerivation.DIRECT and promoted.expires_at is None


async def test_a_familiar_address_needs_gmails_passed_check_to_be_a_known_sender() -> None:
    """Anyone can write a familiar address in a From line. A passed sender check shows
    the mail came with that address's domain's authority; `none` shows nothing."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.7, messages=[_message(source, check="none")])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.statement.startswith("Email from Alex Rivera")
    assert memory.derivation is MemoryDerivation.HYPOTHESIS
    assert memory.confidence <= 0.4 and memory.flagged_for_review
    # It is still kept by its thread's importance, as every sender's mail is.
    assert memory.expires_at == source.sent_at + timedelta(days=365)

    # The owner's mark lengthens it; no re-rating promotes it without the check.
    await seed_feedback(factory, "important")
    report = await service.rerate_thread("work", "t-mail")
    assert (report.promoted, report.by_retention) == (0, {"no_expiry": 1})
    assert (await service.rerate_all(apply=True)).promoted == 0
    assert (await stored(factory, memory)).derivation is MemoryDerivation.HYPOTHESIS

    # Read again with the check passed, the same message is from someone the owner knows.
    await seed_thread(factory, priority=0.7, messages=[_message(source, check="pass")])
    assert (await service.rerate_thread("work", "t-mail")).promoted == 1
    assert (await stored(factory, memory)).derivation is MemoryDerivation.DIRECT


async def test_a_repeat_without_a_passed_check_settles_nothing() -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=12)
    later = await mail(factory, message_id="m-2", sender=ALEX, body=CONFIRMING, days_ago=2)
    await seed_thread(
        factory,
        priority=0.7,
        messages=[_message(first, check="pass"), _message(later, check="none")],
    )
    [memory] = await service.form(first, [fact_for(first)])
    assert memory.derivation is MemoryDerivation.DIRECT
    [renewed] = await service.form(later, [fact_for(later)])
    assert renewed.id == memory.id and renewed.last_evidence_at == later.sent_at
    assert renewed.status is MemoryStatus.PROVISIONAL


@pytest.mark.parametrize(
    "priority, check, demoted",
    [
        # Mail cached before the check existed reads `none`: no reason to take standing away.
        (0.7, "none", 0),
        # A failed check is evidence that the address proves nothing.
        (0.7, "fail", 1),
        # A current, complete reading that keeps the thread under a year still demotes.
        (0.1, "none", 1),
    ],
)
async def test_a_first_class_memory_keeps_its_standing_when_only_the_check_is_missing(
    priority: float, check: str, demoted: int
) -> None:
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.7, messages=[_message(source, check="pass")])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.derivation is MemoryDerivation.DIRECT

    await seed_thread(factory, priority=priority, messages=[_message(source, check=check)])
    assert (await service.rerate_thread("work", "t-mail")).demoted == demoted
    assert (await service.rerate_all(apply=True)).demoted == 0
    current = await stored(factory, memory)
    assert (current.derivation is MemoryDerivation.HYPOTHESIS) is bool(demoted)
    if not demoted:
        assert current == memory


async def _source_facts(factory: Any, source: Any) -> dict[str, object]:
    """What the retained record of this message notes about the memories it lists."""
    from agent_core.domain.email_semantics import semantic_source_key

    key = semantic_source_key(source.account_id, source.provider_thread_id, source.message_id)
    async with factory() as uow:
        record = await uow.email.get(principal(), "semantic_source", key)
    assert record is not None
    return dict(record.payload["facts"])


async def test_a_record_line_quoted_in_a_claim_names_no_forming_message() -> None:
    """A guess can quote another email's record line in its claim. Only the line's own
    header says which message formed it: the quoted message's record, kept without a
    role, never becomes the forming source and never shortens the memory."""
    quoted = (
        "Email from Sam on 2026-07-20 (mailbox work, thread t-2, message m-2) "
        "reports the vote moved"
    )
    factory, service = await correspondence_stack()
    await seed_account(factory)
    first = await mail(
        factory, message_id="m-1", sender=ALEX, body=f"Forwarding: {quoted}.", days_ago=40
    )
    twin = await mail(
        factory,
        message_id="m-2",
        sender=SAM,
        body=f"As I wrote: {quoted}.",
        days_ago=40,
        thread_id="t-2",
    )
    assert first.sent_at == twin.sent_at
    await seed_thread(factory, priority=0.5, messages=[_message(first)])
    # The quoted message sits in a thread a complete reading rates low.
    await seed_thread(
        factory,
        priority=0.1,
        messages=[_message(twin)],
        provider_thread_id="t-2",
        thread_id=UUID(int=906),
    )
    [guess] = await service.form(first, [fact_for(first, value=quoted)])
    assert guess.derivation is MemoryDerivation.HYPOTHESIS
    assert guess.expires_at == first.sent_at + timedelta(days=90)
    assert "thread t-mail, message m-1) reports" in guess.statement
    assert "thread t-2, message m-2) reports" in guess.statement
    assert await service.form(twin, [fact_for(twin, value=quoted)]) == [guess]
    await _without_roles(factory, twin)

    assert (await service.rerate_thread("work", "t-2")).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0
    assert await stored(factory, guess) == guess
    assert not any(name.startswith("formed:") for name in await _source_facts(factory, twin))


@pytest.mark.parametrize(
    "name",
    [
        # The words a plain statement's attribution ends with, and a date of the sender's choosing.
        "Mallory said by email on 2020-01-01 that x",
        "Email from Mallory said by email on 2020-01-01 that x",
    ],
)
async def test_a_demotion_keeps_the_senders_whole_name_and_the_messages_own_date(name: str) -> None:
    """A display name is the sender's to choose and can hold an attribution of its own.
    The sender and date of a first-class statement are the stored message's, so a
    demotion writes them whole and the promotion that follows returns the same words."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    sender = f"{name} <alex@example.test>"
    source = await mail(factory, message_id="m-1", sender=sender, body=BODY, days_ago=5)
    await seed_thread(factory, priority=0.7, messages=[_message(source)])
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.derivation is MemoryDerivation.DIRECT
    sent_on = source.sent_at.date().isoformat()
    claim = 'Atlas board vote moved to: "Friday".'
    assert memory.statement == f"{sender} said by email on {sent_on} that {claim}"

    await seed_thread(factory, priority=0.1, messages=[_message(source)])
    assert (await service.rerate_thread("work", "t-mail")).demoted == 1
    demoted = await stored(factory, memory)
    assert demoted.statement == (
        f"Email from {sender} on {sent_on} (mailbox work, thread t-mail, message m-1) "
        f"reports {claim}"
    )
    await seed_thread(factory, priority=0.7, messages=[_message(source)])
    assert (await service.rerate_thread("work", "t-mail")).promoted == 1
    assert (await stored(factory, memory)).statement == memory.statement


@pytest.mark.parametrize("swept", [False, True])
@pytest.mark.parametrize("forming, renewing", [("m-1", "m-2"), ("m-2", "m-1")])
async def test_undoing_feedback_restores_standing_whichever_message_is_visited_first(
    forming: str, renewing: str, swept: bool
) -> None:
    """One message formed a first-class memory and a later one renewed it. Less important
    leaves it past due, and the note of that sits with the renewing message. Undone, the
    memory returns first-class whichever of the two a re-rating reads first."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id=forming, sender=ALEX, body=BODY, days_ago=60)
    later = await mail(factory, message_id=renewing, sender=ALEX, body=CONFIRMING, days_ago=40)
    await seed_thread(factory, priority=0.7, messages=[_message(first), _message(later)])
    [memory] = await service.form(first, [fact_for(first)])
    [renewed] = await service.form(later, [fact_for(later)])
    assert renewed.id == memory.id and renewed.derivation is MemoryDerivation.DIRECT
    assert renewed.last_evidence_at == later.sent_at
    assert renewed.expires_at == later.sent_at + timedelta(days=365)

    await seed_feedback(factory, "less_important")
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert report.by_retention == {"30_days": 1}
    lapsed = await stored(factory, memory)
    assert lapsed.expires_at == later.sent_at + timedelta(days=30) and lapsed.expires_at < NOW
    assert f"lapsed:{memory.id}" in await _source_facts(factory, later)
    if swept:
        [closed] = await service._governed.expire()
        assert closed.id == memory.id and closed.status is MemoryStatus.EXPIRED

    await seed_feedback(factory, "less_important", undone=True)
    preview = await service.rerate_thread("work", "t-mail", apply=False, cause="feedback")
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert preview.model_dump(exclude={"applied"}) == report.model_dump(exclude={"applied"})
    assert (report.first_class, report.guesses, report.by_retention) == (1, 0, {"1_year": 1})
    restored = await stored(factory, memory)
    assert restored.derivation is MemoryDerivation.DIRECT
    assert restored.statement == renewed.statement
    assert restored.expires_at == renewed.expires_at
    assert restored.status is renewed.status and restored.valid_to is None
    # Nothing is left to do, by the hook or by the owner's command.
    assert (await service.rerate_thread("work", "t-mail", cause="feedback")).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0


@pytest.mark.parametrize("swept", [False, True])
@pytest.mark.parametrize(
    "formed_in, forming, renewed_in, renewing",
    # The command reads retained messages in key order: the renewing message's key
    # sorts first in one case and the forming message's in the other.
    [("t-mail", "m-1", "t-2", "m-2"), ("t-2", "m-2", "t-mail", "m-1")],
)
async def test_the_owners_command_restores_standing_across_threads_in_either_order(
    formed_in: str, forming: str, renewed_in: str, renewing: str, swept: bool
) -> None:
    """The message that formed a memory and the one that renewed it can sit in two
    threads. Whichever the command reads first, the memory it puts back takes the
    standing its forming message now earns."""
    from agent_core.domain.email_semantics import semantic_source_key

    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(
        factory, message_id=forming, sender=ALEX, body=BODY, days_ago=60, thread_id=formed_in
    )
    later = await mail(
        factory,
        message_id=renewing,
        sender=ALEX,
        body=CONFIRMING,
        days_ago=40,
        thread_id=renewed_in,
    )
    assert (
        semantic_source_key("work", formed_in, forming)
        < semantic_source_key("work", renewed_in, renewing)
    ) is (formed_in == "t-2")
    ids = {"t-mail": THREAD_ID, "t-2": UUID(int=906)}
    for source, thread in ((first, formed_in), (later, renewed_in)):
        await seed_thread(
            factory,
            priority=0.7,
            messages=[_message(source)],
            provider_thread_id=thread,
            thread_id=ids[thread],
        )
    [memory] = await service.form(first, [fact_for(first)])
    [renewed] = await service.form(later, [fact_for(later)])
    assert renewed.id == memory.id and renewed.derivation is MemoryDerivation.DIRECT

    # Less important on the sender rates both threads: the forming thread demotes the
    # memory, and the renewing thread's own reading leaves it past due.
    lowered = EmailFeedback(
        id=UUID(int=910),
        thread_id=THREAD_ID,
        target="person",
        judgment="less_important",
        target_values=["alex@example.test"],
        created_at=NOW,
    )
    await _put(factory, "feedback", str(lowered.id), lowered.model_dump(mode="json"))
    assert (await service.rerate_thread("work", formed_in, cause="feedback")).demoted == 1
    assert (await service.rerate_thread("work", renewed_in)).by_retention == {"30_days": 1}
    lapsed = await stored(factory, memory)
    assert lapsed.derivation is MemoryDerivation.HYPOTHESIS
    assert lapsed.expires_at is not None and lapsed.expires_at < NOW
    if swept:
        await service._governed.expire()

    undone = lowered.model_copy(update={"undone_at": NOW})
    await _put(factory, "feedback", str(undone.id), undone.model_dump(mode="json"))
    preview = await service.rerate_all(apply=False)
    report = await service.rerate_all(apply=True)
    assert preview.model_dump(exclude={"applied"}) == report.model_dump(exclude={"applied"})
    assert (report.first_class, report.guesses, report.by_retention) == (1, 0, {"1_year": 1})
    restored = await stored(factory, memory)
    assert restored.derivation is MemoryDerivation.DIRECT
    assert restored.statement == renewed.statement and restored.expires_at == renewed.expires_at
    assert restored.status is renewed.status and restored.valid_to is None
    assert (await service.rerate_all(apply=True)).changed == 0


@pytest.mark.parametrize("swept", [False, True])
async def test_a_memory_put_back_from_another_thread_takes_its_forming_threads_standing(
    swept: bool,
) -> None:
    """Less important on a person, given on the thread that formed a memory, demotes it
    there; the thread that renewed it leaves it past due at its next assessment. Undone,
    the forming thread is read first and finds the memory still past due. The pass over
    the other thread puts it back and reads the message its record line names, so the
    memory returns first-class. What that pass may not do is take standing away."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    first = await mail(factory, message_id="m-1", sender=ALEX, body=BODY, days_ago=60)
    later = await mail(
        factory, message_id="m-2", sender=ALEX, body=CONFIRMING, days_ago=40, thread_id="t-2"
    )
    other = UUID(int=906)
    await seed_thread(factory, priority=0.7, messages=[_message(first)])
    await seed_thread(
        factory, priority=0.7, messages=[_message(later)], provider_thread_id="t-2", thread_id=other
    )
    [memory] = await service.form(first, [fact_for(first)])
    [renewed] = await service.form(later, [fact_for(later)])
    assert renewed.id == memory.id and renewed.derivation is MemoryDerivation.DIRECT

    async def judge(undone: bool) -> None:
        item = EmailFeedback(
            id=UUID(int=911),
            thread_id=THREAD_ID,
            target="person",
            judgment="less_important",
            target_values=["alex@example.test"],
            created_at=NOW,
            undone_at=NOW if undone else None,
        )
        await _put(factory, "feedback", str(item.id), item.model_dump(mode="json"))

    await judge(undone=False)
    assert (await service.rerate_thread("work", "t-mail", cause="feedback")).demoted == 1
    assert (await service.lengthen_threads([("work", "t-2")])).changed == 0
    assert (await service.rerate_thread("work", "t-2")).by_retention == {"30_days": 1}
    if swept:
        await service._governed.expire()

    await judge(undone=True)
    # The feedback's own thread first, as the request does it, then the other thread.
    assert (await service.rerate_thread("work", "t-mail", cause="feedback")).changed == 0
    report = await service.lengthen_threads([("work", "t-2")])
    assert (report.promoted, report.first_class, report.by_retention) == (1, 1, {"1_year": 1})
    restored = await stored(factory, memory)
    assert restored.derivation is MemoryDerivation.DIRECT
    assert restored.statement == renewed.statement and restored.expires_at == renewed.expires_at
    assert restored.status is renewed.status and restored.valid_to is None

    # The same from the other side. Given on the renewing thread, the judgment leaves a
    # first-class memory past due, and a first-class statement names no message. While
    # it is past due its forming thread is rated low. Put back, it keeps its standing
    # until that thread's own re-rating, which reads the thread whole.
    await judge(undone=False)
    assert (await service.rerate_thread("work", "t-2", cause="feedback")).by_retention == {
        "30_days": 1
    }
    await seed_thread(factory, priority=0.1, messages=[_message(first)])
    await judge(undone=True)
    assert (await service.rerate_thread("work", "t-mail")).changed == 0
    report = await service.rerate_thread("work", "t-2", cause="feedback")
    assert (report.demoted, report.first_class, report.by_retention) == (0, 1, {"1_year": 1})
    report = await service.rerate_thread("work", "t-mail")
    assert report.demoted == 1
    assert (await stored(factory, memory)).derivation is MemoryDerivation.HYPOTHESIS


async def _owed(factory: Any) -> object:
    """Whether the thread's saved assessment still owes its memories a re-rating."""
    async with factory() as uow:
        row = await uow.email.get(principal(), "assessment", str(THREAD_ID))
    return None if row is None else row.payload.get("rerating_pending")


LOW_READING: dict[str, object] = {
    "priority": 0.1,
    "content_importance": 0.1,
    "urgency": 0,
    "grounded": True,
    "source_fingerprint": "rev-1",
    "rerating_pending": True,
}


async def test_a_rating_saved_for_an_earlier_revision_of_the_thread_lowers_nothing() -> None:
    """A grounded low assessment is saved and its re-rating does not run. The thread
    then changes: Gmail drops another message, the rating is reset and the thread is
    still held whole. Followed up now, the saved rating describes a revision that is
    gone. It may lengthen; it never shortens or demotes."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=40)
    await seed_thread(factory, priority=0.7, messages=[_message(source)], fingerprint="rev-1")
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.derivation is MemoryDerivation.DIRECT
    assert memory.expires_at == source.sent_at + timedelta(days=365)

    await _put(factory, "assessment", str(THREAD_ID), LOW_READING)
    await seed_thread(
        factory, priority=0.0, messages=[_message(source)], assessed=False, fingerprint="rev-2"
    )
    assert (await service.rerate_thread("work", "t-mail")).changed == 0
    assert (await service.rerate_all(apply=True)).changed == 0
    assert await stored(factory, memory) == memory
    # The debt stays where the next refresh finds it, until an assessment replaces it.
    assert await _owed(factory) is True

    # What the earlier revision was rated still lengthens.
    await _put(factory, "assessment", str(THREAD_ID), {**LOW_READING, "priority": 0.9})
    assert (await service.rerate_thread("work", "t-mail")).by_retention == {"no_expiry": 1}
    assert await _owed(factory) is True

    # An assessment of the revision now stored is a current reading: it lowers, and settles.
    await _put(
        factory, "assessment", str(THREAD_ID), {**LOW_READING, "source_fingerprint": "rev-2"}
    )
    report = await service.rerate_thread("work", "t-mail")
    assert (report.demoted, report.by_retention) == (1, {"30_days": 1})
    assert await _owed(factory) is None


@pytest.mark.parametrize("change", ["feedback", "sender"])
async def test_a_stale_rating_leaves_the_owner_and_the_sender_check_their_say(change: str) -> None:
    """The owner's own judgment and a failed sender check are not ratings of a revision.
    Each still acts while the saved rating describes an earlier one."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=40)
    await seed_thread(factory, priority=0.7, messages=[_message(source)], fingerprint="rev-1")
    [memory] = await service.form(source, [fact_for(source)])
    await _put(factory, "assessment", str(THREAD_ID), LOW_READING)
    check = "fail" if change == "sender" else "pass"
    await seed_thread(
        factory,
        priority=0.0,
        messages=[_message(source, check=check)],
        assessed=False,
        fingerprint="rev-2",
    )
    if change == "feedback":
        await seed_feedback(factory, "less_important")
        report = await service.rerate_thread("work", "t-mail", cause="feedback")
        assert (report.demoted, report.by_retention) == (1, {"30_days": 1})
    else:
        report = await service.rerate_thread("work", "t-mail")
        assert (report.demoted, report.by_retention) == (1, {"1_year": 1})
        assert (await stored(factory, memory)).expires_at == memory.expires_at


async def _pause(factory: Any, paused: bool) -> None:
    """The owner pauses or resumes learning, as the mailbox stores it."""
    from agent_core.domain.email import EmailLearningState

    state = EmailLearningState(paused=paused)
    await _put(factory, "learning", "shared", state.model_dump(mode="json"))


async def test_an_assessments_rerating_reads_the_learning_pause_under_its_own_lock() -> None:
    """A refresh reads the pause before it calls the re-rating, and the owner can pause
    in between. A re-rating that follows an assessment reads it again once it holds the
    mailbox lock, and while learning is paused it writes nothing: no memory, no note,
    no settled mark."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY)
    await seed_thread(factory, priority=0.2, messages=[_message(source)], fingerprint="rev-1")
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.expires_at == source.sent_at + timedelta(days=30)
    await seed_thread(factory, priority=0.9, messages=[_message(source)], fingerprint="rev-1")
    await _put(factory, "assessment", str(THREAD_ID), {**LOW_READING, "priority": 0.9})

    await _pause(factory, True)
    for apply in (False, True):
        report = await service.rerate_thread("work", "t-mail", apply=apply)
        assert (report.examined, report.changed) == (0, 0)
    assert await stored(factory, memory) == memory and await _owed(factory) is True

    await _pause(factory, False)
    assert (await service.rerate_thread("work", "t-mail")).by_retention == {"no_expiry": 1}
    assert (await stored(factory, memory)).expires_at is None and await _owed(factory) is None


async def test_the_owners_feedback_reaches_memories_while_learning_is_paused() -> None:
    """A pause holds what Veetbot learns on its own. A mark is the owner's own act: it
    reaches the thread's memories at once, pause or not, so Important given during a
    pause still keeps a memory from expiring, and Less important and its undo work as
    they always do, with the note that lets the undo put the memory back."""
    factory, service = await correspondence_stack()
    await know_alex(factory, service)
    source = await mail(factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=40)
    await seed_thread(factory, priority=0.7, messages=[_message(source)], fingerprint="rev-1")
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.derivation is MemoryDerivation.DIRECT
    assert memory.expires_at == source.sent_at + timedelta(days=365)
    await _put(factory, "assessment", str(THREAD_ID), {**LOW_READING, "priority": 0.7})
    await _pause(factory, True)

    # Less important: the assessment's own re-rating is held, the owner's is not.
    await seed_feedback(factory, "less_important")
    held = await service.rerate_thread("work", "t-mail")
    assert (held.examined, held.changed) == (0, 0)
    assert await stored(factory, memory) == memory and await _owed(factory) is True
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert (report.demoted, report.by_retention) == (1, {"30_days": 1})
    lapsed = await stored(factory, memory)
    assert lapsed.expires_at == source.sent_at + timedelta(days=30) and lapsed.expires_at < NOW
    assert f"lapsed:{memory.id}" in await _source_facts(factory, source)

    # Undone in the same pause, the memory is put back as it was.
    await seed_feedback(factory, "less_important", undone=True)
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert (report.promoted, report.by_retention) == (1, {"1_year": 1})
    restored = await stored(factory, memory)
    assert restored.derivation is MemoryDerivation.DIRECT
    assert restored.statement == memory.statement and restored.expires_at == memory.expires_at
    assert f"lapsed:{memory.id}" not in await _source_facts(factory, source)

    # Important removes the expiry at once.
    await seed_feedback(factory, "important")
    report = await service.rerate_thread("work", "t-mail", cause="feedback")
    assert report.by_retention == {"no_expiry": 1}
    assert (await stored(factory, memory)).expires_at is None


async def test_the_pass_over_a_persons_other_threads_runs_while_learning_is_paused() -> None:
    """Important on a person is the owner's own act in every thread it rates: the pass
    over the other threads lengthens their memories during a pause as at any other time."""
    factory, service = await correspondence_stack()
    await seed_account(factory)
    source = await mail(
        factory, message_id="m-in", sender=ALEX, body=BODY, days_ago=20, thread_id="t-2"
    )
    await seed_thread(factory, priority=0.2, messages=[_message(source)], provider_thread_id="t-2")
    [memory] = await service.form(source, [fact_for(source)])
    assert memory.expires_at == source.sent_at + timedelta(days=30)
    marked = EmailFeedback(
        id=UUID(int=912),
        thread_id=UUID(int=904),  # given on another of Alex's threads
        target="person",
        judgment="important",
        target_values=["alex@example.test"],
        created_at=NOW,
    )
    await _put(factory, "feedback", str(marked.id), marked.model_dump(mode="json"))

    await _pause(factory, True)
    report = await service.lengthen_threads([("work", "t-2")])
    assert (report.changed, report.by_retention) == (1, {"no_expiry": 1})
    assert (await stored(factory, memory)).expires_at is None
