"""Formation pipeline units: eligibility, resolution, provenance, and corrections.

These cover the write-path mechanisms specified in
docs/plan/memory-formation-and-consolidation.md that the Milestone 9 gates
exercise only end to end: the deterministic candidate extractor, the
portability ceiling, reinforcement arithmetic, cross-project promotion,
typed rejections, and tombstone replay.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.context import Fact, WorkingState
from agent_core.domain.errors import ConflictError, ToolValidationError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    BeliefType,
    DecayResult,
    MemoryAuthority,
    MemoryEdit,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RecallResult,
    RejectionKind,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.memory.formation import (
    MAX_INFERRED_CONFIDENCE,
    DeterministicCandidateExtractor,
    GovernedMemoryService,
    portability_ceiling,
)
from agent_core.memory.profiles import DecayProfile, FormationProfile, UsageDeltas
from agent_core.memory.retrieval import HybridMemoryRetriever
from tests.contract.memory_fixtures import (
    formation_stack,
    recall_query,
    session_events,
    user_event,
)
from tests.contract.support import NOW, PRINCIPAL_ID, SESSION_ID, TENANT, principal, session


def _envelope(event_type: str, payload: dict[str, object], sequence: int = 1) -> EventEnvelope:
    return EventEnvelope(
        id=sequence,
        session_id=SESSION_ID,
        run_id=None,
        sequence=sequence,
        event_type=event_type,
        payload_schema_version=1,
        actor_type="principal",
        actor_id=PRINCIPAL_ID,
        payload=dict(payload),
        trace_id=None,
        created_at=NOW,
    )


async def _form(
    factory: MemoryUnitOfWorkFactory,
    service: GovernedMemoryService,
    statement: str,
    *,
    subject: str = "deploy gating",
    scope: str = "project-a",
    belief_type: BeliefType = BeliefType.FACT,
    portability: Portability | None = None,
    explicit: bool = True,
    authority: MemoryAuthority = MemoryAuthority.USER,
    confidence: float | None = None,
) -> MemoryRecord:
    sequence = await user_event(factory, statement)
    return await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement=statement,
        subject=subject,
        scope=scope,
        belief_type=belief_type,
        portability=portability,
        source_event_ids=[sequence],
        explicit=explicit,
        authority=authority,
        confidence=confidence,
    )


async def _memory_event_types(factory: MemoryUnitOfWorkFactory) -> list[str]:
    return [
        event.event_type
        for event in await session_events(factory)
        if event.event_type.startswith("memory.")
    ]


def test_portability_ceiling_travels_with_belief_type() -> None:
    assert portability_ceiling(BeliefType.PREFERENCE) is Portability.PORTABLE
    assert portability_ceiling(BeliefType.USER_MODEL_ATTR) is Portability.PORTABLE
    assert portability_ceiling(BeliefType.PROCEDURE_POINTER) is Portability.PORTABLE
    assert portability_ceiling(BeliefType.FACT) is Portability.CONTEXTUAL
    assert portability_ceiling(BeliefType.RELATIONSHIP) is Portability.CONTEXTUAL


async def test_candidate_extracts_explicit_remember_requests() -> None:
    event = _envelope(
        "user.message.created", {"content": "Remember that my launch code is ORBIT-9"}
    )
    candidates = await DeterministicCandidateExtractor().extract(
        [event], principal=principal(), scope="project-a"
    )

    assert len(candidates) == 1
    assert candidates[0].subject == "user"
    assert candidates[0].statement == "my launch code is ORBIT-9"
    assert candidates[0].belief_type is BeliefType.FACT


async def test_multiword_activity_context_preserves_the_recent_subject() -> None:
    part = TextPart(
        text=(
            "I've started playing the soprano saxophone. On soprano saxophone "
            "my right thumb is often hurting after half an hour of playing."
        )
    ).model_dump(mode="json")
    candidates = await DeterministicCandidateExtractor().extract(
        [_envelope("user.message.created", {"content": [part]})],
        principal=principal(),
        scope="general",
    )

    pain = next(candidate for candidate in candidates if "thumb pain" in candidate.subject)
    assert pain.subject == "right thumb pain while playing soprano saxophone"
    assert (
        pain.statement
        == "User's right thumb often hurts after half an hour of playing soprano saxophone."
    )


async def test_candidate_extracts_stated_preferences_from_text_parts() -> None:
    part = TextPart(text="We really prefer tabs over spaces").model_dump(mode="json")
    candidates = await DeterministicCandidateExtractor().extract(
        [_envelope("user.message.created", {"content": [part]})],
        principal=principal(),
        scope="project-a",
    )

    assert len(candidates) == 1
    assert candidates[0].subject == "indentation style"
    assert candidates[0].statement == "User prefers tabs over spaces."
    assert candidates[0].belief_type is BeliefType.PREFERENCE


async def test_candidate_ignores_non_user_events_and_plain_chatter() -> None:
    tool_event = _envelope("tool.call.completed", {"content": "Remember that x is y"})
    chatter = _envelope("user.message.created", {"content": "What time is it?"})
    candidates = await DeterministicCandidateExtractor().extract(
        [tool_event, chatter], principal=principal(), scope="project-a"
    )

    assert candidates == []


async def test_remember_rejects_portability_above_type_ceiling() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    sequence = await user_event(factory, "The build is gated on CI.")
    with pytest.raises(ToolValidationError):
        await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement="Deploys are gated on green CI.",
            subject="deploy gating",
            scope="project-a",
            belief_type=BeliefType.FACT,
            portability=Portability.PORTABLE,
            source_event_ids=[sequence],
        )
    assert await service.list_memories() == []


async def test_remember_is_idempotent_for_the_same_source_event() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    sequence = await user_event(factory, "Remember that deploys are gated on CI.")
    first = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="Deploys are gated on CI.",
        subject="deploy gating",
        scope="project-a",
        source_event_ids=[sequence],
    )
    second = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="Deploys are gated on CI.",
        subject="deploy gating",
        scope="project-a",
        source_event_ids=[sequence],
    )
    assert second.id == first.id
    assert second.corroboration_count == 1
    assert await _memory_event_types(factory) == ["memory.formed"]


async def test_duplicate_corroboration_reinforces_and_activates_provisional() -> None:
    clock, factory, service, _retriever = await formation_stack()
    first_source = await user_event(factory, "We prefer tabs over spaces")
    formed = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="Prefers tabs over spaces",
        subject="user",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        source_event_ids=[first_source],
        explicit=False,
    )
    assert formed.status is MemoryStatus.PROVISIONAL
    assert formed.confidence == pytest.approx(0.55)

    clock.advance(timedelta(seconds=5))
    second_source = await user_event(factory, "We prefer tabs over spaces")
    reinforced = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="Prefers tabs over spaces",
        subject="user",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        source_event_ids=[second_source],
    )
    assert reinforced.id == formed.id
    assert reinforced.status is MemoryStatus.ACTIVE
    assert reinforced.corroboration_count == 2
    assert reinforced.confidence == pytest.approx(0.65)
    assert reinforced.source_event_ids == sorted({first_source, second_source})
    assert reinforced.last_reinforced_at == clock.now()
    assert await _memory_event_types(factory) == ["memory.formed", "memory.reinforced"]


async def test_cross_project_corroboration_promotes_to_user_scope() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    formed = await _form(
        factory,
        service,
        "Reviews precede merges.",
        subject="review policy",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
    )
    promoted = await _form(
        factory,
        service,
        "Reviews precede merges.",
        subject="review policy",
        scope="project-b",
        belief_type=BeliefType.PREFERENCE,
    )
    assert promoted.id == formed.id
    assert promoted.scope == "user"
    assert promoted.origin_scopes == ["project-a", "project-b"]
    assert promoted.corroboration_count == 2
    assert await _memory_event_types(factory) == ["memory.formed", "memory.promoted"]


async def test_local_beliefs_are_never_promoted_across_projects() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await _form(
        factory,
        service,
        "Staging endpoint is svc.internal.",
        subject="staging endpoint",
        scope="project-a",
        portability=Portability.LOCAL,
    )
    reinforced = await _form(
        factory,
        service,
        "Staging endpoint is svc.internal.",
        subject="staging endpoint",
        scope="project-b",
        portability=Portability.LOCAL,
    )
    assert reinforced.scope == "project-a"
    assert reinforced.origin_scopes == ["project-a", "project-b"]
    events = await _memory_event_types(factory)
    assert "memory.promoted" not in events
    assert events == ["memory.formed", "memory.reinforced"]


async def test_remember_defaults_provenance_to_the_latest_user_message() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "First message")
    latest = await user_event(factory, "Second message")
    belief = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="Deploys are gated on CI.",
        subject="deploy gating",
        scope="project-a",
    )
    assert belief.source_event_ids == [latest]


async def test_remember_requires_a_user_source_event() -> None:
    _clock, _factory, service, _retriever = await formation_stack()
    with pytest.raises(ToolValidationError):
        await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement="Deploys are gated on CI.",
            subject="deploy gating",
            scope="project-a",
        )


async def test_remember_rejects_provenance_naming_missing_events() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "A real episode")
    with pytest.raises(ToolValidationError):
        await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement="Deploys are gated on CI.",
            subject="deploy gating",
            scope="project-a",
            source_event_ids=[41],
        )


async def test_deletion_tombstone_blocks_case_and_whitespace_variants() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    belief = await _form(factory, service, "User prefers concise answers")
    await service.delete(belief.id)
    sequence = await user_event(factory, "Please remember it again")
    with pytest.raises(ConflictError):
        await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement="  user   PREFERS   concise ANSWERS ",
            subject="deploy gating",
            scope="project-a",
            source_event_ids=[sequence],
        )


async def test_delete_keeps_only_a_content_hash_tombstone() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    statement = "User prefers concise answers"
    belief = await _form(factory, service, statement)
    await service.delete(belief.id)
    async with factory() as uow:
        rejections = await uow.memories.outstanding_rejections(TENANT, PRINCIPAL_ID)
    assert [rejection.kind for rejection in rejections] == [RejectionKind.DELETED]
    assert rejections[0].statement is None
    expected = hashlib.sha256(statement.casefold().encode()).hexdigest()
    assert rejections[0].statement_sha256 == expected
    assert await service.list_memories(include_inactive=True) == []
    assert "memory.deleted" in await _memory_event_types(factory)


async def test_not_here_rejection_lowers_portability_without_retiring() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    belief = await _form(
        factory,
        service,
        "Reviews precede merges.",
        subject="review policy",
        belief_type=BeliefType.PREFERENCE,
    )
    updated = await service.reject(belief.id, RejectionKind.NOT_HERE)
    assert updated.portability is Portability.LOCAL
    assert updated.status is MemoryStatus.ACTIVE
    assert updated.valid_to is None
    assert updated.flagged_for_review is True
    assert updated.confidence == pytest.approx(belief.confidence - 0.2)


async def test_unspecified_rejection_downweights_without_retiring() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    belief = await _form(factory, service, "User prefers concise answers")
    updated = await service.reject(belief.id, RejectionKind.UNSPECIFIED)
    assert updated.status is MemoryStatus.ACTIVE
    assert updated.valid_to is None
    assert updated.flagged_for_review is True
    assert updated.confidence == pytest.approx(belief.confidence - 0.2)
    async with factory() as uow:
        rejections = await uow.memories.outstanding_rejections(TENANT, PRINCIPAL_ID)
    assert [rejection.kind for rejection in rejections] == [RejectionKind.UNSPECIFIED]
    assert rejections[0].statement == belief.statement


async def test_changed_rejection_requires_replacement_text() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    belief = await _form(factory, service, "User prefers concise answers")
    with pytest.raises(ToolValidationError):
        await service.reject(belief.id, RejectionKind.CHANGED)


async def test_edit_reattributes_to_user_authority_and_audits() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    belief = await _form(
        factory,
        service,
        "Deploys are gated on CI.",
        authority=MemoryAuthority.INFERRED,
    )
    edited = await service.edit(belief.id, MemoryEdit(statement="Deploys wait for green CI."))
    assert edited.statement == "Deploys wait for green CI."
    assert edited.authority is MemoryAuthority.USER
    assert edited.source_event_ids == belief.source_event_ids
    assert "memory.edited" in await _memory_event_types(factory)


async def test_formation_events_carry_the_belief_payload() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    belief = await _form(factory, service, "Deploys are gated on CI.")
    formed = [
        event for event in await session_events(factory) if event.event_type == "memory.formed"
    ]
    assert len(formed) == 1
    payload_belief = formed[0].payload["belief"]
    assert isinstance(payload_belief, dict)
    assert payload_belief["id"] == str(belief.id)
    assert formed[0].actor_type == "principal"


async def _second_session(factory: MemoryUnitOfWorkFactory) -> UUID:
    """Open a second session, whose event sequence starts at one again."""

    later = session().model_copy(update={"id": UUID(int=0x5E55)})
    async with factory() as uow:
        await uow.sessions.create(later)
    return later.id


async def _session_event(factory: MemoryUnitOfWorkFactory, session_id: UUID, text: str) -> int:
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=PRINCIPAL_ID,
                payload={"content": text},
            )
        )
    return event.sequence


async def test_a_later_session_supersedes_across_colliding_source_sequences() -> None:
    """Event sequences are per session, so equal numbers are not one source.

    The same-source shortcut exists to make a replay of already-consolidated
    episodes a no-op. A second session numbers its first event one as well, so
    comparing bare sequence numbers made every second session look like a
    replay and left the superseded belief live.

    A colliding sequence carries no ordering, so the clock is what orders the
    two statements: the later session speaks a second after the first one, and
    the same instant instead would leave the two in conflict.
    """

    clock, factory, service, _retriever = await formation_stack()
    first_source = await user_event(factory, "I live in Seattle.")
    formed = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User lives in Seattle.",
        subject="home location",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[first_source],
    )

    clock.advance(timedelta(seconds=1))
    later = await _second_session(factory)
    second_source = await _session_event(factory, later, "I live in Portland now.")
    assert second_source == first_source

    superseding = await service.remember(
        session_id=later,
        run_id=None,
        statement="User lives in Portland.",
        subject="home location",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[second_source],
    )

    assert superseding.id != formed.id
    assert superseding.statement == "User lives in Portland."
    async with factory() as uow:
        stored = await uow.memories.get(formed.id, principal())
    assert stored.status is MemoryStatus.SUPERSEDED
    assert stored.superseded_by == superseding.id

    # Replaying the later session's own episode is still a no-op.
    replay = await service.remember(
        session_id=later,
        run_id=None,
        statement="User lives in Portland (restated).",
        subject="home location",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[second_source],
    )
    assert replay.id == superseding.id
    assert replay.statement == "User lives in Portland."


async def test_replaying_an_older_session_does_not_supersede_a_newer_belief() -> None:
    """Re-derivation must never revert currency.

    `agent memory replay --session <id> --confirm` re-consolidates one
    session's original evidence, so the statement it proposes is exactly as old
    as that evidence however long ago it was spoken. Anchoring the
    cross-session recency test on the consolidation instant made every replay
    look later than the belief that had already replaced it, and the stale
    statement superseded the current one. The incoming instant is the newest
    source event backing the candidate, so replaying older evidence against a
    newer successor is a conflict: the newer belief keeps standing, the
    replayed statement enters beside it, both are flagged, and the user is
    asked which holds.
    """

    clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "I live in Seattle.")
    first = await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)
    (seattle,) = first.beliefs

    clock.advance(timedelta(minutes=5))
    later = await _second_session(factory)
    await _session_event(factory, later, "I live in Portland.")
    second = await service.run(trigger="session_idle", scope="general", session_id=later)
    (portland,) = second.beliefs
    assert second.run.superseded == 1

    clock.advance(timedelta(minutes=5))
    replayed = await service.replay(SESSION_ID)

    records = {record.id: record for record in await service.list_memories(include_inactive=True)}
    # The newer belief is untouched by the replay: nothing retires it.
    assert records[portland.id].status is MemoryStatus.PROVISIONAL
    assert records[portland.id].superseded_by is None
    assert records[portland.id].valid_to is None
    # The stale belief stays retired, and the evidence behind it re-enters
    # beside the current statement as a flagged conflict instead of replacing
    # it or being silently dropped.
    assert records[seattle.id].status is MemoryStatus.SUPERSEDED
    (restated,) = replayed.beliefs
    assert restated.statement == seattle.statement
    assert restated.conflicts_with == [portland.id]
    assert restated.flagged_for_review is True
    assert records[portland.id].conflicts_with == [restated.id]
    assert records[portland.id].flagged_for_review is True

    assert replayed.conflicted == 1
    assert replayed.run.committed == 1
    assert replayed.run.superseded == 0
    assert (await _memory_event_types(factory)).count("memory.needs_confirmation") == 1
    assert "memory.superseded" not in await _memory_event_types(factory)


async def test_inferred_contradiction_of_a_user_belief_is_conflicted_flagged_and_linked_both_ways() -> (  # noqa: E501
    None
):
    """An inference never overwrites what the user said; it is linked beside it.

    Both records stay live, each names the other, both are flagged for review,
    and the belief that was already stored takes a fresh store position so the
    next turn's recall delta shows it reappearing with its new marker.
    """

    clock, factory, service, _retriever = await formation_stack()
    stated = await user_event(factory, "Concise answers, please.")
    belief = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers concise answers.",
        subject="answer style",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        source_event_ids=[stated],
    )

    clock.advance(timedelta(seconds=1))
    inferred_source = await user_event(factory, "I prefer detailed answers.")
    inferred = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers detailed answers.",
        subject="answer style",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        source_event_ids=[inferred_source],
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        confidence=MAX_INFERRED_CONFIDENCE,
    )

    records = {record.id: record for record in await service.list_memories(include_inactive=True)}
    assert records[belief.id].status is MemoryStatus.ACTIVE
    assert records[belief.id].superseded_by is None
    assert records[belief.id].valid_to is None
    assert records[belief.id].conflicts_with == [inferred.id]
    assert records[belief.id].flagged_for_review is True
    assert records[inferred.id].status is MemoryStatus.PROVISIONAL
    assert records[inferred.id].conflicts_with == [belief.id]
    assert records[inferred.id].flagged_for_review is True
    # The re-flagged belief is news for the delta, so it moves above the
    # replacement that was written first.
    assert records[belief.id].store_position > records[inferred.id].store_position
    assert records[belief.id].updated_at == clock.now()


async def test_conflict_emits_needs_confirmation_and_counts_as_committed() -> None:
    """A conflict is a commit that asks for confirmation, not a supersession."""

    clock, factory, service, _retriever = await formation_stack()
    stated = await user_event(factory, "Concise answers, please.")
    await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers concise answers.",
        subject="answer style",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        source_event_ids=[stated],
    )

    clock.advance(timedelta(seconds=1))
    await user_event(factory, "I prefer detailed answers.")
    result = await service.run(trigger="session_idle", scope="project-a", session_id=SESSION_ID)

    assert result.conflicted == 1
    assert result.run.committed == 1
    assert result.run.superseded == 0
    assert result.run.rejected == 0
    assert await _memory_event_types(factory) == [
        "memory.formed",
        "memory.formed",
        "memory.needs_confirmation",
    ]


async def test_replaying_a_conflicted_session_is_a_no_op() -> None:
    """A conflict pair is convergent: replaying its evidence rebuilds nothing.

    The conflict branch lifts the existing belief above the replacement it just
    wrote, so the ordering that reaches the most recent related belief first
    reaches the belief that was contradicted, not the record already produced
    from this evidence. The same-source shortcut therefore has to be decided
    over every related belief before any of them is acted on.
    """

    clock, factory, service, _retriever = await formation_stack()
    stated = await user_event(factory, "Concise answers, please.")
    await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers concise answers.",
        subject="answer style",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        source_event_ids=[stated],
    )
    clock.advance(timedelta(seconds=1))
    await user_event(factory, "I prefer detailed answers.")
    formed = await service.run(trigger="session_idle", scope="project-a", session_id=SESSION_ID)
    assert formed.conflicted == 1
    before = {record.id: record for record in await service.list_memories(include_inactive=True)}

    replayed = await service.replay(SESSION_ID)

    after = {record.id: record for record in await service.list_memories(include_inactive=True)}
    assert set(after) == set(before)
    assert [record.store_position for record in after.values()] == [
        record.store_position for record in before.values()
    ]
    assert sorted(len(record.conflicts_with) for record in after.values()) == [1, 1]
    assert (await _memory_event_types(factory)).count("memory.needs_confirmation") == 1
    assert replayed.conflicted == 0
    assert replayed.run.committed == 0
    assert replayed.run.superseded == 0
    assert replayed.run.candidates_proposed == 1
    assert replayed.run.rejected == 1
    assert replayed.beliefs == []


async def test_later_user_statement_still_supersedes() -> None:
    """Equal authority with a later source or a later instant is still ordered."""

    clock, factory, service, _retriever = await formation_stack()
    first_source = await user_event(factory, "I live in Seattle.")
    formed = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User lives in Seattle.",
        subject="home location",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[first_source],
    )
    second_source = await user_event(factory, "I live in Portland now.")
    within_session = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User lives in Portland.",
        subject="home location",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[second_source],
    )
    async with factory() as uow:
        stored = await uow.memories.get(formed.id, principal())
    assert stored.status is MemoryStatus.SUPERSEDED
    assert stored.superseded_by == within_session.id
    assert stored.conflicts_with == []

    # A later session brings no later sequence, so the instant is the ordering.
    later = await _second_session(factory)
    clock.advance(timedelta(seconds=1))
    third_source = await _session_event(factory, later, "I live in Boise now.")
    across_sessions = await service.remember(
        session_id=later,
        run_id=None,
        statement="User lives in Boise.",
        subject="home location",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[third_source],
    )
    async with factory() as uow:
        replaced = await uow.memories.get(within_session.id, principal())
    assert replaced.status is MemoryStatus.SUPERSEDED
    assert replaced.superseded_by == across_sessions.id


async def test_service_names_the_candidate_extractor_it_was_configured_with() -> None:
    clock, factory, service, _retriever = await formation_stack()
    renamed = DeterministicCandidateExtractor()
    renamed.name = "provider-assisted-test-v1"
    configured = GovernedMemoryService(
        factory, clock, SequenceIdFactory(), principal(), extractor=renamed
    )

    assert service.extractor_name == DeterministicCandidateExtractor.name
    assert configured.extractor_name == "provider-assisted-test-v1"


async def test_decay_sweep_lowers_unused_provisional_and_retires_below_floor() -> None:
    """An idle provisional belief loses a step; one below the floor retires.

    Both outcomes are written through the reinforcement path and announce
    themselves as events; only the closing one takes a fresh store position.
    """

    clock, factory, service, _retriever = await formation_stack()
    idle = await _form(factory, service, "Deploys are gated on CI.", explicit=False)
    weak = await _form(
        factory,
        service,
        "The staging cluster is us-east-2.",
        subject="staging cluster",
        explicit=False,
        confidence=0.2,
    )
    assert idle.status is MemoryStatus.PROVISIONAL
    assert weak.confidence == pytest.approx(0.2)

    clock.advance(timedelta(days=31))
    result = await service.decay()

    assert (result.decayed, result.retired) == (1, 1)
    beliefs = {belief.id: belief for belief in await service.list_memories(include_inactive=True)}
    decayed = beliefs[idle.id]
    retired = beliefs[weak.id]
    assert decayed.confidence == pytest.approx(0.5)
    assert decayed.status is MemoryStatus.PROVISIONAL
    assert decayed.valid_to is None
    assert decayed.store_position == idle.store_position
    assert decayed.updated_at == clock.now()
    assert retired.confidence == pytest.approx(0.15)
    assert retired.status is MemoryStatus.RETIRED
    assert retired.valid_to == clock.now()
    assert retired.store_position > weak.store_position
    assert sorted(
        event for event in await _memory_event_types(factory) if event != "memory.formed"
    ) == ["memory.decayed", "memory.retired"]


async def test_decay_moves_a_store_position_only_when_it_closes_the_belief() -> None:
    """Lowering confidence keeps the position; retiring the belief takes a new one.

    A session reads a position above its snapshot watermark as a belief formed
    or corrected since, so republishing a belief for having quietly lost a step
    of confidence would report a change to the user that never happened. Being
    closed is that change, and it is the one the correction lines select on.
    """

    clock, factory, service, retriever = await formation_stack()
    idle = await _form(factory, service, "Deploys are gated on CI.", explicit=False)
    weak = await _form(
        factory,
        service,
        "The staging cluster is us-east-2.",
        subject="staging cluster",
        explicit=False,
        confidence=0.2,
    )
    snapshot = await retriever.snapshot(session_id=SESSION_ID, current_scope="project-a")
    assert {idle.id, weak.id} <= {item.belief_id for item in snapshot.items}

    clock.advance(timedelta(days=31))
    assert (await service.decay()) == DecayResult(decayed=1, retired=1)

    beliefs = {belief.id: belief for belief in await service.list_memories(include_inactive=True)}
    assert beliefs[idle.id].confidence == pytest.approx(0.5)
    assert beliefs[idle.id].store_position == idle.store_position
    assert beliefs[weak.id].status is MemoryStatus.RETIRED
    assert beliefs[weak.id].store_position > snapshot.watermark

    corrections = await retriever.corrections(
        snapshot_id=snapshot.trace_id, watermark=snapshot.watermark
    )
    assert [item.belief_id for item in corrections] == [weak.id]


async def test_decay_sweep_skips_active_user_stated_and_recently_reinforced() -> None:
    """Explicit user belief and a freshly corroborated one are both untouched.

    The first is active at high confidence, which the sweep never selects; the
    second is low enough to select but was reinforced inside its time constant,
    which is what reinforcement resetting decay means.
    """

    clock, factory, service, _retriever = await formation_stack()
    stated = await _form(factory, service, "Deploys are gated on CI.")
    reinforced = await _form(
        factory,
        service,
        "Retries are capped at three.",
        subject="retry policy",
        explicit=False,
        confidence=0.3,
    )
    clock.advance(timedelta(days=31))
    sequence = await user_event(factory, "Retries are capped at three.")
    corroborated = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="Retries are capped at three.",
        subject="retry policy",
        scope="project-a",
        source_event_ids=[sequence],
        explicit=False,
    )
    assert corroborated.id == reinforced.id
    assert corroborated.confidence < MAX_INFERRED_CONFIDENCE
    clock.advance(timedelta(days=2))

    result = await service.decay()

    assert (result.decayed, result.retired) == (0, 0)
    beliefs = {belief.id: belief for belief in await service.list_memories()}
    assert beliefs[stated.id].confidence == pytest.approx(0.9)
    assert beliefs[stated.id].status is MemoryStatus.ACTIVE
    assert beliefs[reinforced.id] == corroborated


async def test_decay_sweep_is_idempotent_within_interval() -> None:
    """One belief loses one step per sweep interval, however often it runs."""

    clock, factory, service, _retriever = await formation_stack()
    idle = await _form(factory, service, "Deploys are gated on CI.", explicit=False)
    clock.advance(timedelta(days=31))

    first = await service.decay()
    once = (await service.list_memories())[0]
    again = await service.decay()
    twice = (await service.list_memories())[0]

    assert (first.decayed, first.retired) == (1, 0)
    assert (again.decayed, again.retired) == (0, 0)
    assert twice == once
    assert once.confidence == pytest.approx(0.5)
    assert once.id == idle.id

    # The guard is a window, not a stop: the next interval decays it again.
    clock.advance(timedelta(days=2))
    later = await service.decay()

    assert (later.decayed, later.retired) == (1, 0)
    assert (await service.list_memories())[0].confidence == pytest.approx(0.45)


async def test_decay_sweep_reaches_the_oldest_idle_belief_past_its_window() -> None:
    """A store larger than one sweep's ceiling still decays its oldest belief.

    The window holds `max_per_sweep` beliefs, and a sweep that filled it with
    the most recently written ones would never reach a long-idle belief: decay
    hands every belief it touches a fresh store position, so the newest rows
    are exactly the ones the sweep has already dealt with. One belief older
    than the ceiling's worth of newer ones proves the window is ordered by
    idleness instead.
    """

    clock, factory, _service, _retriever = await formation_stack()
    ceiling = 3
    service = GovernedMemoryService(
        factory,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(7_000, 8_000)),
        principal(),
        formation_profile=FormationProfile(decay=DecayProfile(max_per_sweep=ceiling)),
    )
    idle = await _form(factory, service, "Deploys are gated on CI.", explicit=False)

    clock.advance(timedelta(days=31))
    newer = [
        await _form(
            factory,
            service,
            f"Region {index} is eu-west-{index}.",
            subject=f"region {index}",
            explicit=False,
        )
        for index in range(ceiling)
    ]

    result = await service.decay()

    beliefs = {belief.id: belief for belief in await service.list_memories()}
    assert (result.decayed, result.retired) == (1, 0)
    assert beliefs[idle.id].confidence == pytest.approx(0.5)
    assert [beliefs[belief.id] for belief in newer] == newer


def _citable_ids() -> SequenceIdFactory:
    """Identifiers whose eight-hex prefixes differ, as production's random ones do.

    A counting sequence renders every belief as `[m:00000000]`, which no
    citation could tell apart; these carry the counter in the leading bytes so
    the rendered prefix identifies exactly one belief and mixes hex letters,
    which is what makes the case-sensitivity of the citation form observable.
    """

    return SequenceIdFactory(
        UUID(int=((0xAA000000 + value) << 96) | value) for value in range(1, 200)
    )


async def _recall_turn(retriever: HybridMemoryRetriever, run_id: UUID) -> RecallResult:
    """Recall the stated preferences into one turn's trace."""

    return await retriever.recall(
        recall_query(text="prefers"),
        session_id=SESSION_ID,
        run_id=run_id,
        turn_id=run_id,
    )


async def test_record_usage_marks_cited_raises_utility_and_reinforcement_not_confidence() -> None:
    """A cited belief gains utility and a fresh reinforcement instant, never confidence.

    One turn recalls two beliefs and the answer cites one of them by the short
    identifier the renderer emits, so the trace mark, the utility, and the
    reinforcement instant move for that belief alone while both confidences
    stay exactly where formation left them: usage is evidence about
    usefulness, not about truth.
    """

    clock, factory, _stack, retriever = await formation_stack()
    service = GovernedMemoryService(factory, clock, _citable_ids(), principal())
    cited = await _form(
        factory,
        service,
        "User prefers concise answers",
        subject="answer style",
        belief_type=BeliefType.PREFERENCE,
    )
    uncited = await _form(
        factory,
        service,
        "User prefers dark themes",
        subject="theme",
        belief_type=BeliefType.PREFERENCE,
    )
    run_id = UUID(int=9_101)
    result = await _recall_turn(retriever, run_id)
    assert {item.belief_id for item in result.items} == {cited.id, uncited.id}

    clock.advance(timedelta(days=1))
    feedback = await service.record_usage(
        session_id=SESSION_ID,
        run_id=run_id,
        final_text=f"Keeping it short, as [m:{str(cited.id)[:8]}] asks.",
    )

    assert (feedback.cited, feedback.uncited, feedback.traces) == (1, 1, 1)
    used = await service.get_memory(cited.id)
    assert used.utility == pytest.approx(0.1)
    assert used.last_reinforced_at == clock.now()
    assert used.store_position == cited.store_position
    assert used.confidence == cited.confidence
    assert used.status is cited.status
    assert used.corroboration_count == cited.corroboration_count
    unused = await service.get_memory(uncited.id)
    assert unused.confidence == uncited.confidence
    assert unused.last_reinforced_at == uncited.last_reinforced_at
    assert (await service.get_recall_trace(result.trace_id)).cited == [cited.id]
    assert (await _memory_event_types(factory)).count("memory.cited") == 1


async def test_record_usage_lowers_utility_for_returned_but_uncited() -> None:
    """A belief that keeps winning the ranking without mattering loses utility.

    Nothing in the answer cites either recalled belief, so both fall by the
    profile's uncited delta with their confidence and reinforcement instants
    untouched; two further completions under a profile that overshoots the
    floor show the fall bottoming out at -1 rather than running away, and the
    completion that finds both already at the floor writes nothing at all.
    """

    clock, factory, _stack, retriever = await formation_stack()
    service = GovernedMemoryService(factory, clock, _citable_ids(), principal())
    first = await _form(
        factory,
        service,
        "User prefers concise answers",
        subject="answer style",
        belief_type=BeliefType.PREFERENCE,
    )
    second = await _form(
        factory,
        service,
        "User prefers dark themes",
        subject="theme",
        belief_type=BeliefType.PREFERENCE,
    )
    await _recall_turn(retriever, UUID(int=9_201))

    feedback = await service.record_usage(
        session_id=SESSION_ID,
        run_id=UUID(int=9_201),
        final_text="Nothing here quotes a belief.",
    )

    assert (feedback.cited, feedback.uncited, feedback.traces) == (0, 2, 1)
    for belief in (first, second):
        fallen = await service.get_memory(belief.id)
        assert fallen.utility == pytest.approx(-0.05)
        assert fallen.confidence == belief.confidence
        assert fallen.last_reinforced_at == belief.last_reinforced_at

    steep = GovernedMemoryService(
        factory,
        clock,
        _citable_ids(),
        principal(),
        usage=UsageDeltas(uncited_utility_delta=-1.0),
    )
    steps = []
    for run_id in (UUID(int=9_202), UUID(int=9_203)):
        await _recall_turn(retriever, run_id)
        steps.append(
            await steep.record_usage(
                session_id=SESSION_ID, run_id=run_id, final_text="Still nothing to cite."
            )
        )
    floored = {belief.id: belief for belief in await service.list_memories()}

    assert floored[first.id].utility == pytest.approx(-1.0)
    assert floored[second.id].utility == pytest.approx(-1.0)
    # The floor is where the write stops too: the second overshoot would have
    # changed nothing, so it is not written and not counted.
    assert [(step.uncited, step.ambiguous) for step in steps] == [(2, 0), (0, 0)]

    await _recall_turn(retriever, UUID(int=9_204))
    inert = await steep.record_usage(
        session_id=SESSION_ID, run_id=UUID(int=9_204), final_text="Nothing again."
    )
    assert (inert.cited, inert.uncited) == (0, 0)
    assert {belief.id: belief for belief in await service.list_memories()} == floored


async def test_record_usage_is_idempotent_across_repeated_completion() -> None:
    """The re-entrant completion path cannot count one run's citations twice.

    The second call sees the run's own `memory.cited` event and does nothing,
    so the utilities, the trace, and the event stream are exactly what the
    first call left behind.
    """

    clock, factory, _stack, retriever = await formation_stack()
    service = GovernedMemoryService(factory, clock, _citable_ids(), principal())
    cited = await _form(
        factory,
        service,
        "User prefers concise answers",
        subject="answer style",
        belief_type=BeliefType.PREFERENCE,
    )
    await _form(
        factory,
        service,
        "User prefers dark themes",
        subject="theme",
        belief_type=BeliefType.PREFERENCE,
    )
    run_id = UUID(int=9_301)
    result = await _recall_turn(retriever, run_id)
    final_text = f"As [m:{str(cited.id)[:8]}] says, briefly."

    first = await service.record_usage(session_id=SESSION_ID, run_id=run_id, final_text=final_text)
    settled = {belief.id: belief for belief in await service.list_memories()}
    again = await service.record_usage(session_id=SESSION_ID, run_id=run_id, final_text=final_text)

    assert (first.cited, first.uncited, first.traces) == (1, 1, 1)
    assert (again.cited, again.uncited, again.traces) == (0, 0, 0)
    assert {belief.id: belief for belief in await service.list_memories()} == settled
    assert (await service.get_recall_trace(result.trace_id)).cited == [cited.id]
    assert (await _memory_event_types(factory)).count("memory.cited") == 1


async def test_record_usage_parses_short_ids_from_final_text() -> None:
    """Only the renderer's own form, in its own case and length, is a citation.

    The answer shouts one identifier, truncates another, and invents a third;
    none of them is the eight lower-case hex digits the renderer emits inside
    `[m:...]`, so exactly the one belief written the way memory renders it is
    marked used and the other is treated as returned and unused.
    """

    clock, factory, _stack, retriever = await formation_stack()
    service = GovernedMemoryService(factory, clock, _citable_ids(), principal())
    cited = await _form(
        factory,
        service,
        "User prefers concise answers",
        subject="answer style",
        belief_type=BeliefType.PREFERENCE,
    )
    uncited = await _form(
        factory,
        service,
        "User prefers dark themes",
        subject="theme",
        belief_type=BeliefType.PREFERENCE,
    )
    run_id = UUID(int=9_401)
    result = await _recall_turn(retriever, run_id)
    shouted = str(uncited.id)[:8].upper()
    assert shouted != str(uncited.id)[:8]

    feedback = await service.record_usage(
        session_id=SESSION_ID,
        run_id=run_id,
        final_text=(
            f"[m:{str(cited.id)[:8]}] is cited; [m:{shouted}] is shouted, "
            f"[m:{str(uncited.id)[:7]}] is short, and [m:0123abcd] is nobody's."
        ),
    )

    assert (feedback.cited, feedback.uncited) == (1, 1)
    assert (await service.get_memory(cited.id)).utility == pytest.approx(0.1)
    assert (await service.get_memory(uncited.id)).utility == pytest.approx(-0.05)
    assert (await service.get_recall_trace(result.trace_id)).cited == [cited.id]


async def test_record_usage_reads_the_session_snapshot_trace_too() -> None:
    """A belief cited out of the frozen snapshot is fed back like any other.

    The snapshot is taken once at session open and is not a turn trace, so
    without the caller's snapshot identifier a citation of a belief only the
    prefix carried would look like an invention.
    """

    clock, factory, _stack, retriever = await formation_stack()
    service = GovernedMemoryService(factory, clock, _citable_ids(), principal())
    remembered = await _form(
        factory,
        service,
        "User prefers concise answers",
        subject="answer style",
        belief_type=BeliefType.PREFERENCE,
    )
    snapshot = await retriever.snapshot(session_id=SESSION_ID, current_scope="project-a")
    assert [item.belief_id for item in snapshot.items] == [remembered.id]

    feedback = await service.record_usage(
        session_id=SESSION_ID,
        run_id=UUID(int=9_501),
        final_text=f"The prefix already said [m:{str(remembered.id)[:8]}].",
        snapshot_trace_id=snapshot.trace_id,
    )

    assert (feedback.cited, feedback.uncited, feedback.traces) == (1, 0, 1)
    assert (await service.get_memory(remembered.id)).utility == pytest.approx(0.1)
    assert (await service.get_recall_trace(snapshot.trace_id)).cited == [remembered.id]


async def test_record_usage_leaves_store_positions_where_it_found_them() -> None:
    """Usage moves utility, and a utility move is not news for the recall delta.

    The delta query treats a position above the session's watermark as a
    belief formed or corrected since the snapshot, so handing a belief a fresh
    position for having been read would republish it to the next turn as
    though it had changed. Neither side of the feedback may do that.
    """

    clock, factory, _stack, retriever = await formation_stack()
    service = GovernedMemoryService(factory, clock, _citable_ids(), principal())
    cited = await _form(
        factory,
        service,
        "User prefers concise answers",
        subject="answer style",
        belief_type=BeliefType.PREFERENCE,
    )
    uncited = await _form(
        factory,
        service,
        "User prefers dark themes",
        subject="theme",
        belief_type=BeliefType.PREFERENCE,
    )
    run_id = UUID(int=9_601)
    await _recall_turn(retriever, run_id)

    clock.advance(timedelta(days=1))
    await service.record_usage(
        session_id=SESSION_ID,
        run_id=run_id,
        final_text=f"[m:{str(cited.id)[:8]}] it is.",
    )

    moved = await service.get_memory(cited.id)
    fallen = await service.get_memory(uncited.id)
    assert moved.utility == pytest.approx(0.1)
    assert moved.last_reinforced_at == clock.now()
    assert moved.store_position == cited.store_position
    assert fallen.utility == pytest.approx(-0.05)
    assert fallen.store_position == uncited.store_position


def _twin_prefixed_ids() -> SequenceIdFactory:
    """Identifiers where the first two beliefs render the same eight hex digits.

    Formation mints the audit identifier before the belief's own, so every
    second value here is a belief; the two beliefs that share a prefix are what
    an ambiguous citation looks like, and the sequential identifiers the
    evaluation harness issues are the degenerate case of it.
    """

    twin = 0xAA000001
    return SequenceIdFactory(
        [
            UUID(int=(0xAA000900 << 96) | 900),
            UUID(int=(twin << 96) | 1),
            UUID(int=(0xAA000901 << 96) | 901),
            UUID(int=(twin << 96) | 2),
            UUID(int=(0xAA000902 << 96) | 902),
            UUID(int=(0xAA0000FF << 96) | 3),
            *(UUID(int=((0xAA000000 + value) << 96) | value) for value in range(10, 200)),
        ]
    )


async def test_record_usage_credits_nothing_for_an_ambiguous_short_identifier() -> None:
    """A citation that names two returned beliefs at once names neither.

    Eight hex digits identify a belief only while the run's returned set holds
    one belief that starts with them; the evaluation harness's sequential
    identifiers all render `[m:00000000]`, and crediting every returned belief
    for one citation would manufacture the usage evidence a live arm is meant
    to measure. The ambiguous pair is left exactly as it was — neither
    credited nor charged for going unused — and counted as ambiguous, while a
    citation of an unambiguous belief is honoured in the same breath.
    """

    clock, factory, _stack, retriever = await formation_stack()
    service = GovernedMemoryService(factory, clock, _twin_prefixed_ids(), principal())
    twin = await _form(
        factory,
        service,
        "User prefers concise answers",
        subject="answer style",
        belief_type=BeliefType.PREFERENCE,
    )
    other_twin = await _form(
        factory,
        service,
        "User prefers short summaries",
        subject="summary length",
        belief_type=BeliefType.PREFERENCE,
    )
    distinct = await _form(
        factory,
        service,
        "User prefers dark themes",
        subject="theme",
        belief_type=BeliefType.PREFERENCE,
    )
    assert str(twin.id)[:8] == str(other_twin.id)[:8]
    assert str(distinct.id)[:8] != str(twin.id)[:8]
    run_id = UUID(int=9_701)
    result = await _recall_turn(retriever, run_id)
    assert {item.belief_id for item in result.items} == {twin.id, other_twin.id, distinct.id}

    feedback = await service.record_usage(
        session_id=SESSION_ID,
        run_id=run_id,
        final_text=f"Both [m:{str(twin.id)[:8]}] and [m:{str(distinct.id)[:8]}] apply.",
    )

    assert (feedback.cited, feedback.uncited, feedback.ambiguous) == (1, 0, 1)
    assert (await service.get_memory(distinct.id)).utility == pytest.approx(0.1)
    assert (await service.get_memory(twin.id)).utility == 0
    assert (await service.get_memory(other_twin.id)).utility == 0
    assert (await service.get_recall_trace(result.trace_id)).cited == [distinct.id]
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, 0, principal())
    citation = next(event for event in events if event.event_type == "memory.cited")
    assert citation.payload["cited"] == [str(distinct.id)]
    assert citation.payload["uncited"] == []
    assert citation.payload["ambiguous"] == 1


def _fact(statement: str, sources: list[int]) -> Fact:
    """One working-state fact as the manager stamps it: never self-trusted."""

    return Fact(
        statement=statement,
        source_event_ids=sources,
        trust_level=TrustLevel.EXTERNAL_UNTRUSTED,
        established_at=NOW,
    )


async def _working_state_event(
    factory: MemoryUnitOfWorkFactory,
    facts: list[Fact] | None = None,
    *,
    state: object | None = None,
) -> int:
    """Append the working-state update a run's control tool would have written."""

    payload = (
        state
        if state is not None
        else WorkingState(established_facts=list(facts or ())).model_dump(mode="json")
    )
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="context.working_state.updated",
                actor_type="runtime",
                payload={"working_state": payload, "source": "control_tool"},
            )
        )
    return event.sequence


async def test_established_facts_with_trusted_sources_become_affirmed_provisional_candidates() -> (
    None
):
    """A fact the run established from a user event forms at affirmed authority.

    The statement is not first person, so the deterministic extractor proposes
    nothing and the only belief in the result is the one the working state
    contributed: provisional, capped at the inferred confidence, carrying the
    fact's own provenance rather than the whole window's.
    """

    _clock, factory, service, _retriever = await formation_stack()
    source = await user_event(factory, "The Helios deploy gate requires two approvals.")
    await _working_state_event(
        factory,
        [_fact("The Helios deploy gate requires two approvals.", [source])],
    )

    result = await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)

    assert [(belief.subject, belief.statement) for belief in result.beliefs] == [
        ("Helios", "The Helios deploy gate requires two approvals.")
    ]
    formed = result.beliefs[0]
    assert formed.authority is MemoryAuthority.AFFIRMED
    assert formed.status is MemoryStatus.PROVISIONAL
    assert formed.confidence == MAX_INFERRED_CONFIDENCE
    assert formed.belief_type is BeliefType.FACT
    assert formed.portability is portability_ceiling(BeliefType.FACT)
    assert formed.source_event_ids == [source]
    assert formed.scope == "general"
    assert (result.run.candidates_proposed, result.run.committed, result.run.rejected) == (1, 1, 0)


async def test_established_facts_with_untrusted_or_foreign_sources_are_rejected() -> None:
    """Trust is derived at selection, so only owning-principal user events qualify.

    Three facts name a model turn, another principal's message, and a trusted
    event mixed with the model turn. Every one fails the subset test and is
    never proposed, while the fourth fact — sourced from the owning
    principal's own message — proves the pass was running the whole time.
    """

    _clock, factory, service, _retriever = await formation_stack()
    trusted = await user_event(factory, "The Helios deploy gate requires two approvals.")
    async with factory() as uow:
        model_turn = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="assistant.message.completed",
                actor_type="model",
                payload={"content": "The Ares cluster is owned by the platform team."},
            )
        )
        foreign = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id="another-principal",
                payload={"content": "The Nyx budget is fifty thousand dollars."},
            )
        )
    await _working_state_event(
        factory,
        [
            _fact("The Ares cluster is owned by the platform team.", [model_turn.sequence]),
            _fact("The Nyx budget is fifty thousand dollars.", [foreign.sequence]),
            _fact("The Hydra queue is drained nightly.", [trusted, model_turn.sequence]),
            _fact("The Helios deploy gate requires two approvals.", [trusted]),
        ],
    )

    result = await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)

    assert [belief.subject for belief in result.beliefs] == ["Helios"]
    assert (result.run.candidates_proposed, result.run.committed, result.run.rejected) == (1, 1, 0)


async def test_established_facts_count_toward_the_twelve_candidate_ceiling() -> None:
    """Facts are prepended, so they may displace extractor proposals.

    Twelve owned devices and two established facts propose fourteen
    candidates for twelve commit slots. The two facts take the first two, the
    last two extractor proposals are displaced, and the displaced pair is
    counted as rejected rather than silently dropped.
    """

    _clock, factory, service, _retriever = await formation_stack()
    owned = " and ".join(f"a Device-{index}" for index in range(1, 13))
    await user_event(factory, f"I have {owned}.")
    source = await user_event(
        factory,
        "The Helios deploy gate requires two approvals. Ares rollbacks need a signed manifest.",
    )
    await _working_state_event(
        factory,
        [
            _fact("The Helios deploy gate requires two approvals.", [source]),
            _fact("Ares rollbacks need a signed manifest.", [source]),
        ],
    )

    result = await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)

    assert (result.run.candidates_proposed, result.run.committed, result.run.rejected) == (
        14,
        12,
        2,
    )
    assert [belief.subject for belief in result.beliefs[:2]] == ["Helios", "Ares"]
    assert [belief.authority for belief in result.beliefs[:2]] == [
        MemoryAuthority.AFFIRMED,
        MemoryAuthority.AFFIRMED,
    ]
    assert [belief.subject for belief in result.beliefs[2:]] == [
        f"Device-{index}" for index in range(1, 11)
    ]
    assert all(belief.authority is MemoryAuthority.INFERRED for belief in result.beliefs[2:])


async def test_malformed_working_state_event_is_skipped() -> None:
    """A working-state payload that will not validate costs the run nothing."""

    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "I have an Apple Watch.")
    await _working_state_event(factory, state={"established_facts": [{"statement": "no sources"}]})

    result = await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)

    assert [belief.subject for belief in result.beliefs] == ["Apple Watch"]
    assert result.beliefs[0].authority is MemoryAuthority.INFERRED
    assert (result.run.candidates_proposed, result.run.committed, result.run.rejected) == (1, 1, 0)


async def test_established_facts_are_ignored_when_the_profile_disables_them() -> None:
    """The pass is behind `formation.established_facts_enabled`."""

    clock, factory, _service, _retriever = await formation_stack()
    service = GovernedMemoryService(
        factory,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(7_000, 7_200)),
        principal(),
        formation_profile=FormationProfile(established_facts_enabled=False),
    )
    source = await user_event(factory, "The Helios deploy gate requires two approvals.")
    await _working_state_event(
        factory,
        [_fact("The Helios deploy gate requires two approvals.", [source])],
    )

    result = await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)

    assert result.beliefs == []
    assert (result.run.candidates_proposed, result.run.committed, result.run.rejected) == (0, 0, 0)
