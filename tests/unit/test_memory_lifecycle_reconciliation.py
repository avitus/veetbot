"""Lifecycle reconciliation and owner confirmation (ADR-0247, plan ADR-A).

Decay keeps its horizon from ``last_evidence_at`` (adaptive-memory-distillation.md)
but takes a step from an automatically captured durable owner statement only
with evidence of non-use: it was delivered to the model at least
``decay.min_unused_deliveries`` times without being cited. An affirmed direct
belief is outside automatic decay altogether. ``confirm`` restores or
strengthens a belief as the owner's own statement through formation, never by
editing status in place.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.errors import ConflictError, ToolValidationError
from agent_core.domain.memory import (
    USAGE_ACCOUNTING_VERSION,
    BeliefType,
    MemoryAuthority,
    MemoryDerivation,
    MemoryLongevity,
    MemoryRecord,
    MemoryReviewOutcome,
    MemoryStatus,
    RejectionKind,
)
from agent_core.memory.formation import GovernedMemoryService
from tests.contract.memory_fixtures import formation_stack, session_events, user_event
from tests.contract.support import SESSION_ID, principal


async def _form(
    factory: MemoryUnitOfWorkFactory,
    service: GovernedMemoryService,
    statement: str,
    *,
    subject: str,
    explicit: bool = False,
    authority: MemoryAuthority = MemoryAuthority.INFERRED,
    confidence: float | None = None,
) -> MemoryRecord:
    sequence = await user_event(factory, statement)
    return await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement=statement,
        subject=subject,
        scope="project-a",
        belief_type=BeliefType.FACT,
        source_event_ids=[sequence],
        explicit=explicit,
        authority=authority,
        confidence=confidence,
    )


async def _set(factory: MemoryUnitOfWorkFactory, record: MemoryRecord, **update: object) -> None:
    async with factory() as uow:
        await uow.memories.reinforce(record.model_copy(update=update, deep=True))


async def _get(factory: MemoryUnitOfWorkFactory, belief_id: UUID) -> MemoryRecord:
    async with factory() as uow:
        return await uow.memories.get(belief_id, principal())


def _delivered(delivered: int, cited: int) -> dict[str, object]:
    return {
        "delivered_count": delivered,
        "cited_count": cited,
        "usage_accounting_version": USAGE_ACCOUNTING_VERSION,
    }


# --- decay -----------------------------------------------------------------


async def test_decay_steps_a_durable_owner_statement_only_with_evidence_of_non_use() -> None:
    """INFERRED + DIRECT + DURABLE: unseen is not unused.

    Three automatically captured owner statements sit idle past the fact
    horizon. The one delivered three times and never cited loses a step; the one
    never delivered and the one cited once in three deliveries are untouched.
    A tentative hypothesis keeps today's rule and decays from its evidence clock.
    """

    clock, factory, service, _retriever = await formation_stack()
    unused = await _form(factory, service, "Deploys are gated on CI.", subject="deploy gating")
    unseen = await _form(factory, service, "Staging runs in us-east-2.", subject="staging")
    cited = await _form(factory, service, "Retries are capped at three.", subject="retries")
    hypothesis = await _form(factory, service, "The team may move to Rust.", subject="language")
    assert unused.derivation is MemoryDerivation.DIRECT
    assert unused.longevity is MemoryLongevity.DURABLE
    await _set(factory, unused, **_delivered(3, 0))
    await _set(factory, cited, **_delivered(3, 1))
    await _set(
        factory,
        hypothesis,
        derivation=MemoryDerivation.HYPOTHESIS,
        longevity=MemoryLongevity.TENTATIVE,
    )

    clock.advance(timedelta(days=31))
    result = await service.decay()

    assert (result.decayed, result.retired) == (2, 0)
    after = {record.id: record for record in await service.list_memories(include_inactive=True)}
    assert after[unused.id].confidence == pytest.approx(unused.confidence - 0.05)
    assert after[hypothesis.id].confidence == pytest.approx(hypothesis.confidence - 0.05)
    assert after[unseen.id].confidence == pytest.approx(unseen.confidence)
    assert after[cited.id].confidence == pytest.approx(cited.confidence)
    assert after[unseen.id].updated_at == unseen.updated_at
    assert after[cited.id].updated_at == cited.updated_at


async def test_decay_never_touches_affirmed_direct_or_user_beliefs() -> None:
    """An owner-confirmed conclusion is corrected by review, never by idleness."""

    clock, factory, service, _retriever = await formation_stack()
    affirmed = await _form(factory, service, "Andy confirmed the CEO list.", subject="ceo list")
    await _set(
        factory,
        affirmed,
        authority=MemoryAuthority.AFFIRMED,
        status=MemoryStatus.ACTIVE,
        confidence=0.5,
        **_delivered(9, 0),
    )
    stated = await _form(
        factory,
        service,
        "Remove Galileo from the tracked companies.",
        subject="tracked companies",
        authority=MemoryAuthority.USER,
    )
    await _set(factory, stated, status=MemoryStatus.PROVISIONAL, confidence=0.3, **_delivered(9, 0))

    clock.advance(timedelta(days=400))
    assert (await service.decay()).model_dump() == {"decayed": 0, "retired": 0}
    assert (await _get(factory, affirmed.id)).confidence == pytest.approx(0.5)
    assert (await _get(factory, stated.id)).confidence == pytest.approx(0.3)


# --- confirm ---------------------------------------------------------------


async def test_confirm_supersedes_the_belief_with_the_owners_own_statement() -> None:
    """One unit of work: the confirmation event, the replacement at user
    authority citing it, the predecessor superseded without a rejection."""

    clock, factory, service, _retriever = await formation_stack()
    before = await _form(
        factory, service, "Matthew Scullion is CEO of Matillion.", subject="scullion"
    )
    await _set(factory, before, authority=MemoryAuthority.AFFIRMED, confidence=0.4)

    clock.advance(timedelta(minutes=1))
    after = await service.review(before.id, MemoryReviewOutcome.CONFIRM)

    assert after.id != before.id
    assert after.statement == before.statement
    assert (after.subject, after.scope, after.belief_type) == (
        before.subject,
        before.scope,
        before.belief_type,
    )
    assert (after.portability, after.sensitivity) == (before.portability, before.sensitivity)
    assert after.authority is MemoryAuthority.USER
    assert after.status is MemoryStatus.ACTIVE
    assert after.expires_at is None
    assert after.confidence == pytest.approx(0.9)
    events = await session_events(factory)
    confirmation = [event for event in events if event.event_type == "memory.owner_confirmation"]
    assert len(confirmation) == 1
    assert confirmation[0].actor_type == "principal"
    assert confirmation[0].payload == {"belief_id": str(before.id), "statement": before.statement}
    assert after.source_event_ids == [confirmation[0].sequence]
    predecessor = await _get(factory, before.id)
    assert predecessor.status is MemoryStatus.SUPERSEDED
    assert predecessor.superseded_by == after.id
    assert predecessor.valid_to == clock.now()
    assert predecessor.store_position > before.store_position
    async with factory() as uow:
        assert (
            await uow.memories.outstanding_rejections(
                predecessor.tenant_id, predecessor.principal_id
            )
            == []
        )
    types = [event.event_type for event in events if event.event_type.startswith("memory.")]
    assert "memory.rejected" not in types
    assert types[-3:] == ["memory.owner_confirmation", "memory.superseded", "memory.formed"]


async def test_confirm_restores_a_decay_retired_belief_and_the_replacement_never_decays() -> None:
    clock, factory, service, _retriever = await formation_stack()
    retired = await _form(
        factory,
        service,
        "Remove Galileo from the tracked companies.",
        subject="tracked",
        confidence=0.2,
    )
    await _set(factory, retired, **_delivered(3, 0))
    clock.advance(timedelta(days=31))
    assert (await service.decay()).retired == 1
    assert (await _get(factory, retired.id)).status is MemoryStatus.RETIRED

    restored = await service.review(retired.id, MemoryReviewOutcome.CONFIRM)
    assert restored.status is MemoryStatus.ACTIVE
    assert restored.authority is MemoryAuthority.USER
    assert (await _get(factory, retired.id)).superseded_by == restored.id

    await _set(factory, restored, **_delivered(50, 0))
    clock.advance(timedelta(days=400))
    assert (await service.decay()).model_dump() == {"decayed": 0, "retired": 0}
    assert (await _get(factory, restored.id)).confidence == pytest.approx(0.9)


async def test_confirm_lifts_a_live_duplicate_to_user_authority() -> None:
    """Decay retired a belief; automatic capture later formed the same words
    again at inferred authority with an expiry. Confirming the retired belief
    meets that duplicate in formation, which reinforces it rather than forming
    a twin, and the reinforced row is then the owner's statement: user
    authority, active, direct, durable, no automatic expiry, never decayed."""

    clock, factory, service, _retriever = await formation_stack()
    statement = "Remove Galileo from the tracked companies."
    retired = await _form(factory, service, statement, subject="tracked", confidence=0.2)
    await _set(factory, retired, **_delivered(3, 0))
    clock.advance(timedelta(days=31))
    assert (await service.decay()).retired == 1
    sequence = await user_event(factory, statement)
    recaptured = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement=statement,
        subject="tracked",
        scope="project-a",
        belief_type=BeliefType.FACT,
        source_event_ids=[sequence],
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        longevity=MemoryLongevity.ONGOING,
    )
    assert recaptured.id != retired.id
    assert recaptured.expires_at is not None

    restored = await service.review(retired.id, MemoryReviewOutcome.CONFIRM)

    assert restored.id == recaptured.id
    assert restored.authority is MemoryAuthority.USER
    assert restored.status is MemoryStatus.ACTIVE
    assert restored.derivation is MemoryDerivation.DIRECT
    assert restored.longevity is MemoryLongevity.DURABLE
    assert restored.expires_at is None
    assert await _get(factory, restored.id) == restored
    assert (await _get(factory, retired.id)).superseded_by == restored.id

    await _set(factory, restored, **_delivered(50, 0))
    clock.advance(timedelta(days=400))
    assert (await service.decay()).model_dump() == {"decayed": 0, "retired": 0}
    assert (await _get(factory, restored.id)).status is MemoryStatus.ACTIVE


async def test_confirm_refuses_tombstones_corrections_and_replacement_text() -> None:
    clock, factory, service, _retriever = await formation_stack()
    deleted = await _form(factory, service, "The office is in Palo Alto.", subject="office")
    twin = await _form(factory, service, "The office is in Palo Alto.", subject="office two")
    await service.delete(deleted.id)
    with pytest.raises(ConflictError):
        await service.review(twin.id, MemoryReviewOutcome.CONFIRM)

    untrue = await _form(factory, service, "The office has a gym.", subject="gym")
    await service.reject(untrue.id, RejectionKind.UNTRUE)
    with pytest.raises(ConflictError):
        await service.review(untrue.id, MemoryReviewOutcome.CONFIRM)

    changed = await _form(factory, service, "Standup is at nine.", subject="standup")
    await service.reject(
        changed.id, RejectionKind.CHANGED, replacement_statement="Standup is at ten."
    )
    with pytest.raises(ConflictError):
        await service.review(changed.id, MemoryReviewOutcome.CONFIRM)

    live = await _form(factory, service, "Lunch is at noon.", subject="lunch")
    with pytest.raises(ToolValidationError):
        await service.review(
            live.id, MemoryReviewOutcome.CONFIRM, replacement_statement="Lunch is at one."
        )
    assert (await _get(factory, live.id)).status is MemoryStatus.PROVISIONAL
    del clock


async def test_confirm_is_a_no_op_on_replay_within_one_turn_of_events() -> None:
    """Confirming the replacement again forms nothing new beyond one more
    owner statement: the predecessor is already superseded and refused."""

    _clock, factory, service, _retriever = await formation_stack()
    before = await _form(factory, service, "Backups run nightly.", subject="backups")
    after = await service.review(before.id, MemoryReviewOutcome.CONFIRM)
    with pytest.raises(ConflictError):
        await service.review(before.id, MemoryReviewOutcome.CONFIRM)
    assert (await _get(factory, after.id)).status is MemoryStatus.ACTIVE
    assert (await _get(factory, before.id)).superseded_by == after.id
