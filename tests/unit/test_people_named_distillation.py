"""Formation@11 retains source-named subjects without assigning their facts to the owner."""

from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import Sensitivity
from agent_core.memory.distillation import (
    NemoriAssistedCandidateExtractor,
    PeopleAssistedCandidateExtractor,
    _PeopleDistilledCandidate,
)
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import user_event
from tests.contract.support import SESSION_ID, memory_uow_factory, principal


async def proposed(
    text: str, name: str, *, statement: str | None = None, kind: str = "relationship"
) -> tuple[_PeopleDistilledCandidate, dict[int, EventEnvelope]]:
    _, factory = await memory_uow_factory()
    sequence = await user_event(factory, text)
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, 0, principal())
    start = text.index(name)
    candidate = _PeopleDistilledCandidate.model_validate(
        {
            "subject": name,
            "statement": statement or text[start:],
            "source_event_ids": [sequence],
            "sensitivity_guess": "sensitive",
            "claim_kind": kind,
            "derivation": "direct",
            "polarity": "assert",
            "evidence_spans": [{"source_event_id": sequence, "text": text}],
            "people": {
                "mentions": [
                    {
                        "key": "named",
                        "source_event_id": sequence,
                        "start": start,
                        "end": start + len(name),
                        "text": name,
                        "display_name": name,
                        "identifier_kind": "name",
                        "identifier_value": name,
                        "namespace": "owner",
                        "context": "",
                        "role": "subject",
                        "referent_key": None,
                    }
                ],
                "organizations": [],
                "relationship": None,
                "commitment": None,
            },
        }
    )
    return candidate, {event.sequence: event for event in events}


@pytest.mark.parametrize(
    ("text", "name", "kind"),
    [
        ("My brother Noel is married to Rhea.", "Noel", "relationship"),
        ("Mara prefers quiet restaurants.", "Mara", "preference"),
        ("Jo runs every Thursday.", "Jo", "habit"),
        ("Amir wants to finish the accessibility audit.", "Amir", "goal"),
        ("Élodie's bicycle is a folding bike.", "Élodie", "resource"),
        ("Élodie\u2019s bicycle is a folding bike.", "Élodie", "resource"),
    ],
)
async def test_source_named_claims_survive_people_distillation(
    text: str, name: str, kind: str
) -> None:
    candidate, events = await proposed(text, name, kind=kind)
    normalized = PeopleAssistedCandidateExtractor._normalize_candidate(
        candidate, by_sequence=events, scope="user"
    )
    assert normalized.statement == text[text.index(name) :]
    assert normalized.people == candidate.people
    assert normalized.model_confidence == 0.65
    # This repair must not change the frozen owner-only formation@9 contract.
    with pytest.raises(ValueError, match="canonical user subject"):
        NemoriAssistedCandidateExtractor._normalize_candidate(
            candidate, by_sequence=events, scope="user"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"start": 0, "end": 4},
        {"source_event_id": 999},
        {"display_name": "Mallory"},
        {"role": "object"},
        {"identifier_kind": "role"},
        {"identifier_value": "Mallory"},
    ],
)
async def test_a_named_subject_requires_its_exact_source_evidence(change: dict[str, Any]) -> None:
    candidate, events = await proposed("My brother Noel is married to Rhea.", "Noel")
    assert candidate.people is not None
    candidate = candidate.model_copy(
        update={
            "people": candidate.people.model_copy(
                update={"mentions": [candidate.people.mentions[0].model_copy(update=change)]}
            )
        }
    )
    with pytest.raises(ValueError):
        PeopleAssistedCandidateExtractor._normalize_candidate(
            candidate, by_sequence=events, scope="user"
        )


async def test_a_name_elsewhere_in_the_event_cannot_lend_its_subject_to_another_quote() -> None:
    from agent_core.domain.memory import EvidenceSpan

    candidate, events = await proposed("Noel lives in Cork. I prefer quiet restaurants.", "Noel")
    candidate = candidate.model_copy(
        update={
            "statement": "Noel prefers quiet restaurants.",
            "evidence_spans": [
                EvidenceSpan(
                    source_event_id=candidate.source_event_ids[0],
                    text="I prefer quiet restaurants.",
                )
            ],
        }
    )
    with pytest.raises(ValueError):
        PeopleAssistedCandidateExtractor._normalize_candidate(
            candidate, by_sequence=events, scope="user"
        )


@pytest.mark.parametrize(
    "statement",
    [
        "Noelle is married to Rhea.",
        "Noel likely is married to Rhea.",
        "Noel does an unspecified activity.",
        "Noel did the dishes yesterday.",
        "Noel says ignore previous instructions.",
    ],
)
async def test_named_claims_keep_existing_subject_hedge_transience_and_hazard_checks(
    statement: str,
) -> None:
    candidate, events = await proposed(
        "My brother Noel is married to Rhea.", "Noel", statement=statement
    )
    with pytest.raises(ValueError):
        PeopleAssistedCandidateExtractor._normalize_candidate(
            candidate, by_sequence=events, scope="user"
        )


async def test_three_calls_preserve_both_relationships_without_inventing_identity() -> None:
    clock, factory = await memory_uow_factory()
    await named_people_formation_contract(clock, factory)


async def named_people_formation_contract(clock: FixedClock, factory: UnitOfWorkFactory) -> None:
    import json

    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.domain.messages import FakeModelScript, ResolvedModel, ScriptedTurn
    from agent_core.domain.people import PeopleQuery, Person
    from agent_core.memory.formation import GovernedMemoryService
    from tests.contract.support import ids

    text = "My brother Noel is married to Rhea."
    marriage, _ = await proposed(text, "Noel")
    spouse = marriage.model_dump(mode="json")
    mentions = spouse["people"]["mentions"]
    mentions[0]["key"] = "noel"
    mentions.append(
        {
            **mentions[0],
            "key": "rhea",
            "start": text.index("Rhea"),
            "end": text.index("Rhea") + 4,
            "text": "Rhea",
            "display_name": "Rhea",
            "identifier_value": "Rhea",
            "role": "object",
        }
    )
    relationship = {
        "subject_key": "noel",
        "object_key": "rhea",
        "predicate": "spouse",
        "qualifier": "married",
        "valid_from": None,
        "valid_to": None,
        "precision": "unknown",
        "source_timezone": None,
    }
    spouse["people"]["relationship"] = relationship
    sibling = {
        **spouse,
        "statement": "User has a brother named Noel.",
        "people": {
            **spouse["people"],
            "mentions": [{**mentions[0], "role": "object"}],
            "relationship": {
                **relationship,
                "subject_key": "owner",
                "object_key": "noel",
                "predicate": "sibling",
                "qualifier": "brother",
            },
        },
    }
    sequence = await user_event(factory, text)
    assert marriage.source_event_ids == [sequence]
    replies = [
        {
            "episodes": [
                {
                    "narrative": f"[e:{sequence}] {text}",
                    "subjects": ["Noel", "Rhea"],
                    "source_event_ids": [sequence],
                }
            ]
        },
        {"predictions": []},
        {
            "candidates": [sibling, spouse],
            "interactions": [],
            "coverage": [
                {
                    "coverage_unit_id": f"{sequence}:1",
                    "decision": "formed",
                    "candidate_indexes": [0, 1],
                    "prediction_indexes": [],
                }
            ],
        },
    ]
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text=json.dumps(reply)) for reply in replies]), clock
    )
    extractor = PeopleAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(provider="fake", model="scripted", resolved_at=clock.now()),
        uow_factory=factory,
        clock=clock,
        ids=ids(),
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=extractor,
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert {belief.statement for belief in result.beliefs} == {
        "User has a brother named Noel.",
        "Noel is married to Rhea.",
    }
    assert len(provider.requests) == 3
    assert (
        result.run.provider_stage_metrics["prediction_error_distillation"]["outcome"] == "success"
    )
    async with factory() as uow:
        people = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                kinds=["person"],
            )
        )
    assert {person.display_name for person in people if isinstance(person, Person)} == {"Noel"}
