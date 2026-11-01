"""Synthetic write-path regressions; names and model numbers are fabricated."""

import pytest

from agent_core.domain.memory import BeliefType, MemoryStatus
from agent_core.memory.formation import DeterministicCandidateExtractor
from tests.contract.memory_fixtures import formation_stack
from tests.contract.support import principal
from tests.unit.test_memory_formation import _envelope, _form


async def test_repeated_resource_with_a_nonessential_subject_modifier_reinforces() -> None:
    _, factory, service, _ = await formation_stack()
    first = await _form(
        factory,
        service,
        "User has a new 2027 Aster S8.",
        subject="new 2027 Aster S8",
        belief_type=BeliefType.USER_MODEL_ATTR,
        explicit=False,
    )
    second = await _form(
        factory,
        service,
        "User has a 2027 Aster S8.",
        subject="2027 Aster S8",
        belief_type=BeliefType.USER_MODEL_ATTR,
        explicit=False,
    )
    assert second.id == first.id
    assert second.status is MemoryStatus.ACTIVE
    assert second.corroboration_count == 2


@pytest.mark.parametrize(
    "statement",
    [
        "User has a 2028 Aster S8.",
        "User no longer has a 2027 Aster S8.",
        "User wants a 2027 Aster S8.",
    ],
)
async def test_resource_similarity_does_not_turn_changes_into_reinforcement(statement: str) -> None:
    _, factory, service, _ = await formation_stack()
    first = await _form(
        factory,
        service,
        "User has a 2027 Aster S8.",
        subject="2027 Aster S8",
        belief_type=BeliefType.USER_MODEL_ATTR,
        explicit=False,
    )
    second = await _form(
        factory,
        service,
        statement,
        subject="vehicle",
        belief_type=BeliefType.USER_MODEL_ATTR,
        explicit=False,
    )
    assert second.id != first.id


async def test_general_child_mention_does_not_supersede_named_child() -> None:
    _, factory, service, _ = await formation_stack()
    first = await _form(
        factory,
        service,
        "User's daughter is Mira.",
        subject="daughter",
        belief_type=BeliefType.RELATIONSHIP,
    )
    await _form(
        factory,
        service,
        "User has at least one daughter.",
        subject="daughter",
        belief_type=BeliefType.RELATIONSHIP,
        explicit=False,
    )
    async with factory() as uow:
        retained = await uow.memories.get(first.id, principal())
    assert retained.status is MemoryStatus.ACTIVE
    assert retained.superseded_by is None


@pytest.mark.parametrize(
    "text", ["Move my Tuesday call.", "Check my X account.", "Discuss my Morgan Reed meeting."]
)
async def test_bare_capitalized_possessive_is_not_a_resource_assertion(text: str) -> None:
    candidates = await DeterministicCandidateExtractor().extract(
        [_envelope("user.message.created", {"content": text})],
        principal=principal(),
        scope="general",
    )
    assert not [
        candidate for candidate in candidates if candidate.belief_type is BeliefType.USER_MODEL_ATTR
    ]


async def test_explicit_resource_assertion_still_forms() -> None:
    candidates = await DeterministicCandidateExtractor().extract(
        [_envelope("user.message.created", {"content": "I drive a 2027 Aster S8."})],
        principal=principal(),
        scope="general",
    )
    assert any("2027 Aster S8" in candidate.statement for candidate in candidates)


@pytest.mark.parametrize("retract", [False, True])
def test_provider_possession_is_an_attribute_while_frozen_policy_stays_unchanged(
    retract: bool,
) -> None:
    from agent_core.memory.distillation import (
        NemoriAssistedCandidateExtractor,
        _PeopleDistilledCandidate,
    )
    from agent_core.memory.people_owner_formation import OwnerPeopleCandidateExtractor

    text = "I no longer own a 2027 Aster S8." if retract else "I own a 2027 Aster S8."
    candidate = _PeopleDistilledCandidate.model_validate(
        {
            "subject": "2027 Aster S8",
            "statement": "User no longer owns a 2027 Aster S8."
            if retract
            else "User owns a 2027 Aster S8.",
            "source_event_ids": [1],
            "sensitivity_guess": "internal",
            "claim_kind": "resource",
            "derivation": "direct",
            "polarity": "retract" if retract else "assert",
            "people": None,
            "evidence_spans": [{"source_event_id": 1, "text": text}],
        }
    )
    events = {1: _envelope("user.message.created", {"content": text})}
    live = OwnerPeopleCandidateExtractor._normalize_candidate(
        candidate, by_sequence=events, scope="general"
    )
    assert live.belief_type is BeliefType.USER_MODEL_ATTR
    frozen = NemoriAssistedCandidateExtractor._normalize_candidate(
        candidate, by_sequence=events, scope="general"
    )
    assert frozen.belief_type is BeliefType.PROCEDURE_POINTER


async def test_the_same_possession_stated_in_another_chat_corroborates() -> None:
    """ADR-0282: an owner who names the same car in three chats has one active car."""
    from uuid import UUID

    from agent_core.domain.events import NewEvent
    from tests.contract.support import PRINCIPAL_ID, session

    _, factory, service, _ = await formation_stack()
    first = await _form(
        factory,
        service,
        "User has a 2027 Aster S8.",
        subject="2027 Aster S8",
        belief_type=BeliefType.USER_MODEL_ATTR,
        explicit=False,
    )
    other_session = UUID("00000000-0000-0000-0000-000000000021")
    async with factory() as uow:
        await uow.sessions.create(session().model_copy(update={"id": other_session}))
        event = await uow.events.append(
            NewEvent(
                session_id=other_session,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=PRINCIPAL_ID,
                payload={"content": "I have a new 2027 Aster S8."},
            )
        )
    second = await service.remember(
        session_id=other_session,
        run_id=None,
        statement="User has a new 2027 Aster S8.",
        subject="new 2027 Aster S8",
        scope="project-a",
        belief_type=BeliefType.USER_MODEL_ATTR,
        portability=None,
        source_event_ids=[event.sequence],
        explicit=False,
        authority=first.authority,
        confidence=None,
    )
    assert second.id == first.id, "A restated possession from another chat is the same belief"
    assert second.status is MemoryStatus.ACTIVE
    assert second.corroboration_count == 2
    assert sorted(second.origin_scopes) == ["project-a"]


async def test_merge_possessions_folds_legacy_duplicates_and_retires_placeholders() -> None:
    """ADR-0282: three provisional copies of one car become one active belief."""
    from uuid import UUID

    from tests.contract.support import principal

    _, factory, service, _ = await formation_stack()
    newest = await _form(
        factory,
        service,
        "User has a 2027 Aster S8.",
        subject="2027 Aster S8",
        belief_type=BeliefType.USER_MODEL_ATTR,
        explicit=False,
    )
    placeholder = await _form(
        factory,
        service,
        "User has a X.",
        subject="X",
        belief_type=BeliefType.USER_MODEL_ATTR,
        explicit=False,
    )
    other = await _form(
        factory,
        service,
        "User has a lawn mower.",
        subject="lawn mower",
        belief_type=BeliefType.USER_MODEL_ATTR,
        explicit=False,
    )
    # The pre-ADR store: the same car formed twice more under other subjects,
    # each its own provisional belief.
    async with factory() as uow:
        legacy = []
        for index, (statement, subject) in enumerate(
            [
                ("User has a new 2027 Aster S8.", "new 2027 Aster S8"),
                ("User has a 2027 Aster S8.", "car"),
            ]
        ):
            copy = newest.model_copy(
                update={
                    "id": UUID(int=9000 + index),
                    "statement": statement,
                    "subject": subject,
                    "store_position": newest.store_position - 1 - index,
                },
                deep=True,
            )
            legacy.append(await uow.memories.upsert_belief(copy))
    dry = await service.merge_possessions(confirm=False)
    assert dry.model_dump() == {
        "groups_examined": 1,
        "beliefs_merged": 2,
        "promoted": 1,
        "placeholders_retired": 1,
        "dry_run": True,
    }
    async with factory() as uow:
        untouched = await uow.memories.get(newest.id, principal())
    assert untouched.status is MemoryStatus.PROVISIONAL, "A dry run writes nothing"
    committed = await service.merge_possessions(confirm=True)
    assert committed.dry_run is False and committed.beliefs_merged == 2
    async with factory() as uow:
        canonical = await uow.memories.get(newest.id, principal())
        folded = [await uow.memories.get(record.id, principal()) for record in legacy]
        gone = await uow.memories.get(placeholder.id, principal())
        kept = await uow.memories.get(other.id, principal())
    assert canonical.status is MemoryStatus.ACTIVE
    assert canonical.corroboration_count == 3
    assert canonical.confidence == pytest.approx(min(1.0, newest.confidence + 0.2))
    assert all(record.status is MemoryStatus.SUPERSEDED for record in folded)
    assert all(record.superseded_by == canonical.id for record in folded)
    assert gone.status is MemoryStatus.RETIRED
    assert kept.status is MemoryStatus.PROVISIONAL, "A lone possession is left alone"
    again = await service.merge_possessions(confirm=True)
    assert again.beliefs_merged == 0 and again.placeholders_retired == 0, "The repair is idempotent"
