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


def _message(source: Any, *, direction: str = "received", check: str = "none") -> EmailMessage:
    return EmailMessage(
        id=source.message_id,
        sender=source.sender,
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
) -> EmailThread:
    """The thread as the mailbox projection stores it, with its assessed importance."""
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
    )
    await _put(factory, "thread", str(thread.id), thread.model_dump(mode="json"))
    await _put(
        factory,
        "thread_source",
        thread_source_key("work", provider_thread_id),
        {"thread_id": str(thread.id)},
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

    assert thread_importance(thread, []) == (0.4, False)
    assert thread_importance(thread, [feedback("important")]) == (1.0, True)
    assert thread_importance(thread, [feedback("less_important")]) == (0.0, False)
    assert thread_importance(thread, [feedback("important", undone_at=NOW)]) == (0.4, False)
    assert thread_importance(thread, [feedback("needs_reply")]) == (0.4, False)


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
    await seed_thread(factory, priority=0.2, messages=[_message(source, direction="sent")])
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
    later = await mail(factory, message_id="m-2", sender=ALEX, body=BODY, days_ago=2)
    await seed_thread(factory, priority=0.4, messages=[_message(first), _message(later)])
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


async def test_older_mail_processed_later_renews_nothing() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    newer = await mail(factory, message_id="m-new", sender=ALEX, body=BODY, days_ago=2)
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
    stranger = await mail(
        factory, message_id="m-2", sender="Sam <sam@example.test>", body=BODY, days_ago=6
    )
    later = await mail(factory, message_id="m-3", sender=ALEX, body=BODY, days_ago=2)
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
