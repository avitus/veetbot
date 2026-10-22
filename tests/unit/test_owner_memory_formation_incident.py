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
