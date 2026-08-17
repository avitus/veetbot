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

import pytest

from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.errors import ConflictError, ToolValidationError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryEdit,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RejectionKind,
)
from agent_core.domain.messages import TextPart
from agent_core.memory.formation import (
    GovernedMemoryService,
    _candidate,
    portability_ceiling,
)
from tests.contract.memory_fixtures import formation_stack, session_events, user_event
from tests.contract.support import NOW, PRINCIPAL_ID, SESSION_ID, TENANT


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


def test_candidate_extracts_explicit_remember_requests() -> None:
    event = _envelope(
        "user.message.created", {"content": "Remember that my launch code is ORBIT-9"}
    )
    extracted = _candidate(event)
    assert extracted is not None
    _, subject, statement, belief_type = extracted
    assert subject == "user"
    assert statement == "my launch code is ORBIT-9"
    assert belief_type is BeliefType.FACT


def test_candidate_extracts_stated_preferences_from_text_parts() -> None:
    part = TextPart(text="We really prefer tabs over spaces").model_dump(mode="json")
    extracted = _candidate(_envelope("user.message.created", {"content": [part]}))
    assert extracted is not None
    _, subject, statement, belief_type = extracted
    assert subject == "user"
    assert statement == "User prefers tabs over spaces."
    assert belief_type is BeliefType.PREFERENCE


def test_candidate_ignores_non_user_events_and_plain_chatter() -> None:
    tool_event = _envelope("tool.call.completed", {"content": "Remember that x is y"})
    chatter = _envelope("user.message.created", {"content": "What time is it?"})
    assert _candidate(tool_event) is None
    assert _candidate(chatter) is None


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
