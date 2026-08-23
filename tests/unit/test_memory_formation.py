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
from agent_core.domain.errors import ConflictError, ToolValidationError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryEdit,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RecallResult,
    RejectionKind,
)
from agent_core.domain.messages import TextPart
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
    """

    _clock, factory, service, _retriever = await formation_stack()
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

    Both outcomes are written through the reinforcement path, so each takes a
    fresh store position and announces itself as an event.
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
    assert decayed.store_position > idle.store_position
    assert decayed.updated_at == clock.now()
    assert retired.confidence == pytest.approx(0.15)
    assert retired.status is MemoryStatus.RETIRED
    assert retired.valid_to == clock.now()
    assert sorted(
        event for event in await _memory_event_types(factory) if event != "memory.formed"
    ) == ["memory.decayed", "memory.retired"]


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
    assert used.store_position > cited.store_position
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
    floor show the fall bottoming out at -1 rather than running away.
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
    for run_id in (UUID(int=9_202), UUID(int=9_203)):
        await _recall_turn(retriever, run_id)
        await steep.record_usage(
            session_id=SESSION_ID, run_id=run_id, final_text="Still nothing to cite."
        )

    assert (await service.get_memory(first.id)).utility == pytest.approx(-1.0)
    assert (await service.get_memory(second.id)).utility == pytest.approx(-1.0)


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
