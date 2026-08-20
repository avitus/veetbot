"""Evaluation-gated provider-assisted memory extraction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import agent_core.config as config_module
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.config import (
    ConfigurationError,
    MemoryProviderExtractionMode,
    load_config_document,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryCandidate,
    Polarity,
    Portability,
    ProviderExtractionEvaluationEvidence,
    Sensitivity,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelEvent,
    ModelPermanentError,
    ModelPricing,
    ModelRequest,
    ModelUsage,
    ResolvedModel,
    ScriptedTurn,
)
from agent_core.memory.formation import DeterministicCandidateExtractor
from agent_core.memory.provider_extraction import (
    ProviderAssistedCandidateExtractor,
    _merge_candidates,
    provider_extraction_evidence_matches,
)
from agent_core.policy.loader import load_ruleset_documents
from tests.contract.memory_fixtures import formation_stack, session_events, user_event
from tests.contract.support import AGENT_ID, NOW, principal
from tests.integration.m2_support import memory_settings


class _BlockingProvider:
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream(
        self,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        del request, resolved, attempt
        self.started.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - keeps this an async generator.
            yield

    async def close(self) -> None:
        return None


def _evidence() -> ProviderExtractionEvaluationEvidence:
    return ProviderExtractionEvaluationEvidence(
        extractor_version="provider-assisted-v2",
        formation_policy_version="formation@4",
        model_policy="fake",
        provider="fake",
        model="scripted",
        policy_profile="default",
        policy_version="default@test",
        build_ref="test-build",
        corpus_sha256="a" * 64,
        sample_count=20,
        positive_case_count=20,
        minimum_supported_case_count=16,
        deterministic_supported_case_count=10,
        provider_supported_case_count=16,
        deterministic_supported_candidates=10,
        provider_supported_candidates=16,
        deterministic_fabricated_candidates=0,
        provider_fabricated_candidates=0,
        deterministic_policy_failures=0,
        provider_policy_failures=0,
        evaluated_at=NOW,
    )


def _runtime_policy_version() -> str:
    settings = memory_settings()
    return load_ruleset_documents(
        load_config_document(settings, "policy/default.yaml"),
        load_config_document(settings, "policy/hardline.yaml"),
    ).policy_version


def test_evaluation_evidence_round_trips_and_must_match_the_compiled_policy_version() -> None:
    evidence = _evidence().model_dump()
    evidence["policy_version"] = "default@profile+hline"

    parsed = ProviderExtractionEvaluationEvidence.model_validate(evidence)

    assert parsed.policy_version == "default@profile+hline"
    assert not provider_extraction_evidence_matches(
        parsed,
        ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        "default",
        "different@policy",
    )


def test_evaluation_evidence_rejects_a_downgraded_positive_coverage_floor() -> None:
    payload = _evidence().model_dump()
    payload["minimum_supported_case_count"] = 1
    payload["provider_supported_case_count"] = 1

    with pytest.raises(ValueError, match="eighty percent"):
        ProviderExtractionEvaluationEvidence.model_validate(payload)


@pytest.mark.parametrize(
    ("episode", "claim", "expected"),
    [
        (
            "My daughter is starting an astronomy club at her high school.",
            {
                "claim_kind": "relationship",
                "subject": "daughter",
                "value": None,
                "context": None,
                "quantity": 1,
                "evidence_quote": "My daughter",
                "proposed_portability": "contextual",
                "sensitivity_guess": "sensitive",
            },
            ("relationship", "daughter", "User has at least one daughter."),
        ),
        (
            "My son's robotics team qualified for the state tournament.",
            {
                "claim_kind": "relationship",
                "subject": "son",
                "value": None,
                "context": None,
                "quantity": 1,
                "evidence_quote": "My son",
                "proposed_portability": "contextual",
                "sensitivity_guess": "sensitive",
            },
            ("relationship", "son", "User has at least one son."),
        ),
        (
            "My wife and I go hiking most weekends.",
            {
                "claim_kind": "relationship",
                "subject": "wife",
                "value": None,
                "context": None,
                "quantity": 1,
                "evidence_quote": "My wife",
                "proposed_portability": "contextual",
                "sensitivity_guess": "sensitive",
            },
            ("relationship", "wife", "User has a wife."),
        ),
        (
            "Both of my daughters want to identify constellations.",
            {
                "claim_kind": "relationship",
                "subject": "daughters",
                "value": None,
                "context": None,
                "quantity": 2,
                "evidence_quote": "Both of my daughters",
                "proposed_portability": "contextual",
                "sensitivity_guess": "sensitive",
            },
            ("relationship", "daughters", "User has at least two daughters."),
        ),
        (
            "As a marine biologist, I spend a lot of time analyzing field samples.",
            {
                "claim_kind": "occupation",
                "subject": "occupation",
                "value": "marine biologist",
                "context": None,
                "quantity": None,
                "evidence_quote": "marine biologist",
                "proposed_portability": "portable",
                "sensitivity_guess": "sensitive",
            },
            ("user_model_attr", "occupation", "User is a marine biologist."),
        ),
        (
            "Most weekends I restore old shortwave radios.",
            {
                "claim_kind": "hobby",
                "subject": "hobby",
                "value": "restore old shortwave radios",
                "context": None,
                "quantity": None,
                "evidence_quote": "restore old shortwave radios",
                "proposed_portability": "portable",
                "sensitivity_guess": "internal",
            },
            ("user_model_attr", "hobby", "User restores old shortwave radios."),
        ),
        (
            "Portland is home for me, although I travel often for work.",
            {
                "claim_kind": "home_location",
                "subject": "home location",
                "value": "Portland",
                "context": None,
                "quantity": None,
                "evidence_quote": "Portland is home for me",
                "proposed_portability": "portable",
                "sensitivity_guess": "sensitive",
            },
            ("user_model_attr", "home location", "User lives in Portland."),
        ),
        (
            "I rely on a screen reader when I use desktop applications.",
            {
                "claim_kind": "accessibility_tool",
                "subject": "accessibility",
                "value": "screen reader",
                "context": None,
                "quantity": None,
                "evidence_quote": "rely on a screen reader",
                "proposed_portability": "portable",
                "sensitivity_guess": "sensitive",
            },
            ("user_model_attr", "accessibility", "User relies on a screen reader."),
        ),
        (
            "I've been studying Japanese for three years.",
            {
                "claim_kind": "language_study",
                "subject": "language study",
                "value": "Japanese",
                "context": None,
                "quantity": None,
                "evidence_quote": "studying Japanese",
                "proposed_portability": "portable",
                "sensitivity_guess": "internal",
            },
            ("user_model_attr", "language study", "User studies Japanese."),
        ),
        (
            "I'm in Pacific time, which matters when we schedule calls.",
            {
                "claim_kind": "time_zone",
                "subject": "time zone",
                "value": "Pacific time",
                "context": None,
                "quantity": None,
                "evidence_quote": "in Pacific time",
                "proposed_portability": "portable",
                "sensitivity_guess": "internal",
            },
            ("user_model_attr", "time zone", "User is in Pacific time."),
        ),
        (
            "Our two cats always sit beside the telescope case.",
            {
                "claim_kind": "pet_ownership",
                "subject": "cats",
                "value": "cats",
                "context": None,
                "quantity": 2,
                "evidence_quote": "two cats",
                "proposed_portability": "portable",
                "sensitivity_guess": "internal",
            },
            ("user_model_attr", "cats", "User has two cats."),
        ),
        (
            "Please keep recipe suggestions vegan; that is how I eat.",
            {
                "claim_kind": "diet",
                "subject": "diet",
                "value": "vegan",
                "context": None,
                "quantity": None,
                "evidence_quote": "vegan",
                "proposed_portability": "portable",
                "sensitivity_guess": "sensitive",
            },
            ("preference", "diet", "User follows a vegan diet."),
        ),
        (
            "For technical explanations, short paragraphs and examples work best for me.",
            {
                "claim_kind": "explanation_style",
                "subject": "answer style",
                "value": "short paragraphs and examples",
                "context": "technical explanations",
                "quantity": None,
                "evidence_quote": "short paragraphs and examples",
                "proposed_portability": "portable",
                "sensitivity_guess": "internal",
            },
            (
                "preference",
                "answer style",
                "User prefers short paragraphs and examples for technical explanations.",
            ),
        ),
        (
            "Friday will be the astronomy club's regular meeting night.",
            {
                "claim_kind": "project_schedule",
                "subject": "meeting schedule",
                "value": "Friday",
                "context": "astronomy club",
                "quantity": None,
                "evidence_quote": "astronomy club's regular meeting night",
                "proposed_portability": "local",
                "sensitivity_guess": "internal",
            },
            (
                "fact",
                "astronomy club meeting",
                "The astronomy club's regular meeting night is Friday.",
            ),
        ),
        (
            "I'm hoping to learn astrophotography this year.",
            {
                "claim_kind": "goal",
                "subject": "goal",
                "value": "learn astrophotography",
                "context": None,
                "quantity": None,
                "evidence_quote": "learn astrophotography",
                "proposed_portability": "portable",
                "sensitivity_guess": "internal",
            },
            ("user_model_attr", "goal", "User wants to learn astrophotography."),
        ),
        (
            "My telescope aperture is 3.5 inches.",
            {
                "claim_kind": "user_attribute",
                "subject": "telescope aperture",
                "value": "3.5 inches",
                "context": None,
                "quantity": None,
                "evidence_quote": "telescope aperture is 3.5 inches",
                "proposed_portability": "contextual",
                "sensitivity_guess": "internal",
            },
            (
                "user_model_attr",
                "telescope aperture",
                "User's telescope aperture is 3.5 inches.",
            ),
        ),
    ],
)
async def test_provider_semantic_claims_render_canonical_memories(
    episode: str,
    claim: dict[str, object],
    expected: tuple[str, str, str],
) -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, episode)
    response = json.dumps(
        {
            "candidates": [
                {
                    **claim,
                    "polarity": "assert",
                    "source_event_ids": [source],
                    "model_confidence": 0.9,
                    "valid_from": None,
                    "expires_hint": None,
                }
            ]
        }
    )
    provider = FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text=response)]), clock)
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(7_000, 7_100)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert [
        (candidate.belief_type.value, candidate.subject, candidate.statement)
        for candidate in candidates
    ] == [expected]


async def test_provider_extractor_uses_bounded_structured_call_and_audits_usage() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "The astronomy club was my daughter's idea.")
    response = json.dumps(
        {
            "candidates": [
                {
                    "claim_kind": "relationship",
                    "subject": "daughter",
                    "value": None,
                    "context": None,
                    "quantity": 1,
                    "evidence_quote": "my daughter",
                    "polarity": "assert",
                    "source_event_ids": [source],
                    "model_confidence": 0.92,
                    "proposed_portability": "contextual",
                    "sensitivity_guess": "sensitive",
                    "valid_from": None,
                    "expires_hint": None,
                }
            ]
        }
    )
    provider = FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text=response)]), clock)
    resolved = ResolvedModel(
        provider="fake",
        model="scripted",
        policy_name="fake",
        resolved_at=NOW,
    )
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=resolved,
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(8_000, 8_100)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert [(item.subject, item.statement) for item in candidates] == [
        ("daughter", "User has at least one daughter.")
    ]
    assert provider.requests[0].tools == []
    schema = provider.requests[0].response_schema
    assert schema is not None
    candidate_schema = schema["$defs"]["_SemanticClaim"]
    assert set(candidate_schema["required"]) == set(candidate_schema["properties"])
    ref_siblings: list[set[str]] = []

    def collect_ref_siblings(value: object) -> None:
        if isinstance(value, dict):
            if "$ref" in value and set(value) != {"$ref"}:
                ref_siblings.append(set(value))
            for nested in value.values():
                collect_ref_siblings(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_ref_siblings(nested)

    collect_ref_siblings(schema)
    assert ref_siblings == []
    assert provider.requests[0].maximum_output_tokens == 4096
    async with factory() as uow:
        audits = await uow.process_events.list("memory.provider_extraction.completed")
    assert len(audits) == 1
    payload = audits[0].payload
    assert payload["agent_id"] == str(AGENT_ID)
    assert payload["agent_version"] == "1.0.0"
    assert payload["policy_profile"] == "default"
    assert payload["policy_version"] == "default@test"
    assert payload["authorized_scope"] == "project-a"
    assert payload["tool_scopes"] == []
    assert payload["budget"]["maximum_model_calls"] == 1
    assert payload["usage"]["input_tokens"] > 0
    assert payload["selected_source_event_ids"] == [source]
    assert payload["candidate_count"] == 1
    assert payload["grounded_candidate_count"] == 1
    assert "astronomy club" not in json.dumps(payload)
    assert "at least one daughter" not in json.dumps(payload)


async def test_provider_extractor_rejects_ungrounded_named_claim_with_valid_source() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "The astronomy club was my daughter's idea.")
    response = json.dumps(
        {
            "candidates": [
                {
                    "claim_kind": "home_location",
                    "subject": "home location",
                    "value": "Paris",
                    "context": None,
                    "quantity": None,
                    "evidence_quote": "astronomy club",
                    "polarity": "assert",
                    "source_event_ids": [source],
                    "model_confidence": 0.95,
                    "proposed_portability": "local",
                    "sensitivity_guess": "internal",
                    "valid_from": None,
                    "expires_hint": None,
                }
            ]
        }
    )
    provider = FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text=response)]), clock)
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(8_100, 8_200)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert [(item.subject, item.statement) for item in candidates] == [
        ("daughter", "User has at least one daughter.")
    ]
    async with factory() as uow:
        audits = await uow.process_events.list("memory.provider_extraction.completed")
    assert audits[0].payload["candidate_count"] == 1
    assert audits[0].payload["grounded_candidate_count"] == 0


@pytest.mark.parametrize(
    ("claim_subject", "evidence_quote"),
    [("wife", "my daughter"), ("daughter", "MY DAUGHTER")],
)
async def test_provider_relationship_claim_requires_exact_grounding(
    claim_subject: str,
    evidence_quote: str,
) -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "The astronomy club was my daughter's idea.")
    response = json.dumps(
        {
            "candidates": [
                {
                    "claim_kind": "relationship",
                    "subject": claim_subject,
                    "value": None,
                    "context": None,
                    "quantity": None,
                    "evidence_quote": evidence_quote,
                    "polarity": "assert",
                    "source_event_ids": [source],
                    "model_confidence": 0.95,
                    "proposed_portability": "contextual",
                    "sensitivity_guess": "sensitive",
                    "valid_from": None,
                    "expires_hint": None,
                }
            ]
        }
    )
    provider = FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text=response)]), clock)
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(8_200, 8_300)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert [(item.subject, item.statement) for item in candidates] == [
        ("daughter", "User has at least one daughter.")
    ]
    async with factory() as uow:
        audits = await uow.process_events.list("memory.provider_extraction.completed")
    assert audits[0].payload["grounded_candidate_count"] == 0


async def test_provider_grounding_accepts_decimal_tokens_present_in_the_source() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "My telescope aperture is 3.5 inches.")
    candidate = MemoryCandidate(
        belief_type=BeliefType.FACT,
        subject="telescope aperture",
        statement="User's telescope aperture is 3.5 inches.",
        polarity=Polarity.ASSERT,
        source_event_ids=[source],
        model_confidence=0.9,
        proposed_scope="project-a",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.INTERNAL,
    )

    assert ProviderAssistedCandidateExtractor._is_grounded(
        candidate,
        await session_events(factory),
        principal(),
        "project-a",
    )


def test_provider_merge_preserves_opposite_polarities_for_conflict_resolution() -> None:
    def candidate(statement: str, polarity: Polarity) -> MemoryCandidate:
        return MemoryCandidate(
            belief_type=BeliefType.USER_MODEL_ATTR,
            subject="watch",
            statement=statement,
            polarity=polarity,
            source_event_ids=[1],
            model_confidence=0.9,
            proposed_scope="project-a",
            proposed_portability=Portability.CONTEXTUAL,
            sensitivity_guess=Sensitivity.INTERNAL,
        )

    asserted = candidate("User has a watch.", Polarity.ASSERT)
    retracted = candidate("User no longer has a watch.", Polarity.RETRACT)

    assert _merge_candidates([asserted], [retracted]) == [asserted, retracted]


def test_provider_merge_preserves_distinct_statements_and_sources() -> None:
    def candidate(statement: str, source_event_ids: list[int]) -> MemoryCandidate:
        return MemoryCandidate(
            belief_type=BeliefType.FACT,
            subject="astronomy club",
            statement=statement,
            source_event_ids=source_event_ids,
            model_confidence=0.9,
            proposed_scope="project-a",
            proposed_portability=Portability.CONTEXTUAL,
            sensitivity_guess=Sensitivity.INTERNAL,
        )

    provider = candidate("User's astronomy club meets weekly.", [1])
    different_statement = candidate("User's astronomy club meets monthly.", [1])
    different_source = candidate("User's astronomy club meets weekly.", [2])

    assert _merge_candidates([provider], [different_statement, different_source]) == [
        provider,
        different_statement,
        different_source,
    ]


async def test_provider_extractor_caps_output_before_call_to_stay_inside_cost_budget() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "The astronomy club was my daughter's idea.")
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text='{"candidates":[]}')]),
        clock,
    )
    resolved = ResolvedModel(
        provider="fake",
        model="scripted",
        policy_name="fake",
        pricing=ModelPricing(
            input_per_mtok=Decimal("5.00"),
            output_per_mtok=Decimal("30.00"),
        ),
        resolved_at=NOW,
    )
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=resolved,
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(8_500, 8_600)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    maximum_output = provider.requests[0].maximum_output_tokens
    assert maximum_output is not None
    assert 0 < maximum_output < 4096


async def test_provider_extractor_refuses_unaffordable_input_before_provider_io() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "The astronomy club was my daughter's idea.")
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text='{"candidates":[]}')]),
        clock,
    )
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            pricing=ModelPricing(
                input_per_mtok=Decimal("100000"),
                output_per_mtok=Decimal("1"),
            ),
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(8_600, 8_700)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )
    assert [(item.subject, item.statement) for item in candidates] == [
        ("daughter", "User has at least one daughter.")
    ]
    assert provider.requests == []
    async with factory() as uow:
        audits = await uow.process_events.list("memory.provider_extraction.failed")
    assert audits[0].payload["outcome"] == "cost_budget_exceeded"


async def test_provider_reported_cost_cannot_be_lowered_by_catalog_pricing() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "The astronomy club was my daughter's idea.")
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text='{"candidates":[]}',
                    usage=ModelUsage(
                        input_tokens=10,
                        output_tokens=5,
                        cost=Decimal("0.06"),
                        provider="fake",
                        model="scripted",
                    ),
                )
            ]
        ),
        clock,
    )
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(8_200, 8_300)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    async with factory() as uow:
        failures = await uow.process_events.list("memory.provider_extraction.failed")
    assert len(failures) == 1
    assert failures[0].payload["usage"]["cost"] == "0.06"


@pytest.mark.parametrize(
    "mode",
    [MemoryProviderExtractionMode.AUTO, MemoryProviderExtractionMode.REQUIRED],
)
async def test_evaluated_provider_extractor_is_activated_by_composition(
    tmp_path: Path,
    mode: MemoryProviderExtractionMode,
) -> None:
    evidence_path = tmp_path / "provider-memory-evidence.json"
    evidence_path.write_text(
        _evidence()
        .model_copy(
            update={
                "model_policy": "fake-balanced",
                "policy_version": _runtime_policy_version(),
            }
        )
        .model_dump_json(),
        encoding="utf-8",
    )
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=mode,
        memory_provider_extraction_evidence=evidence_path,
        artifact_root=tmp_path / "artifacts",
    )
    response = json.dumps(
        {
            "candidates": [
                {
                    "claim_kind": "relationship",
                    "subject": "daughter",
                    "value": None,
                    "context": None,
                    "quantity": 1,
                    "evidence_quote": "my daughter",
                    "polarity": "assert",
                    "source_event_ids": [2],
                    "model_confidence": 0.9,
                    "proposed_portability": "contextual",
                    "sensitivity_guess": "sensitive",
                    "valid_from": None,
                    "expires_hint": None,
                }
            ]
        }
    )

    async with build(
        settings=settings,
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        script=FakeModelScript(turns=[ScriptedTurn(text=response)]),
    ) as app:
        session_id = await app.sessions.create()
        async with app.uow_factory() as uow:
            source = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": "The astronomy club was my daughter's idea."},
                )
            )
        assert source.sequence == 2

        result = await app.memory.run(
            trigger="session_idle",
            scope="general",
            session_id=session_id,
        )

    assert [(item.subject, item.statement) for item in result.beliefs] == [
        ("daughter", "User has at least one daughter.")
    ]
    assert result.run.model.startswith("provider-assisted-v2:fake:scripted")
    assert result.run.policy_version == "formation@4"


async def test_provider_extractor_has_a_non_activating_evaluation_mode() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "The astronomy club was my daughter's idea.")
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text='{"candidates":[]}')]),
        clock,
    )

    extractor = ProviderAssistedCandidateExtractor.for_evaluation(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(9_000, 9_100)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="eval.default",
        policy_version="eval.default@test",
        fallback=DeterministicCandidateExtractor(),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )
    assert [(item.subject, item.statement) for item in candidates] == [
        ("daughter", "User has at least one daughter.")
    ]
    async with factory() as uow:
        audits = await uow.process_events.list("memory.provider_extraction.completed")
    assert audits[0].payload["evaluation_mode"] is True
    assert audits[0].payload["evaluation_build_ref"] is None


async def test_provider_failure_is_audited_and_uses_deterministic_fallback() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "I have an Apple Watch.")
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    fail_with=ModelPermanentError(
                        provider="fake",
                        model="scripted",
                        attempt_id=UUID(int=1),
                        message="provider unavailable",
                    )
                )
            ]
        ),
        clock,
    )
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(9_100, 9_200)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert [(item.subject, item.source_event_ids) for item in candidates] == [
        ("Apple Watch", [source])
    ]
    async with factory() as uow:
        audits = await uow.process_events.list("memory.provider_extraction.failed")
    assert len(audits) == 1
    payload = audits[0].payload
    assert payload["outcome"] == "failed"
    assert payload["error_class"] == "ModelStreamError"
    assert "Apple Watch" not in json.dumps(payload)
    assert "provider unavailable" not in json.dumps(payload)


async def test_input_budget_is_refused_before_provider_io() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "x" * 20_000)
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text='{"candidates":[]}')]),
        clock,
    )
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(9_150, 9_250)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert candidates == []
    assert provider.requests == []
    async with factory() as uow:
        audits = await uow.process_events.list("memory.provider_extraction.failed")
    assert audits[0].payload["outcome"] == "input_budget_exceeded"


async def test_cancellation_is_audited_and_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "The astronomy club was my daughter's idea.")
    provider = _BlockingProvider()
    extractor = ProviderAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name="fake",
            resolved_at=NOW,
        ),
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(UUID(int=value) for value in range(9_175, 9_275)),
        principal=principal(),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        policy_version="default@test",
        evidence=_evidence(),
        fallback=DeterministicCandidateExtractor(),
    )
    original_audit = extractor._audit
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    audit_finished = asyncio.Event()

    async def delayed_audit(**kwargs: object) -> None:
        audit_started.set()
        await release_audit.wait()
        await original_audit(**kwargs)  # type: ignore[arg-type]
        audit_finished.set()

    monkeypatch.setattr(extractor, "_audit", delayed_audit)
    task = asyncio.create_task(
        extractor.extract(
            await session_events(factory),
            principal=principal(),
            scope="project-a",
        )
    )
    await provider.started.wait()

    task.cancel()
    await audit_started.wait()
    task.cancel()
    release_audit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(audit_finished.wait(), timeout=1.0)

    async with factory() as uow:
        audits = await uow.process_events.list("memory.provider_extraction.failed")
    assert audits[0].payload["outcome"] == "cancelled"
    assert audits[0].payload["error_class"] == "CancelledError"


async def test_activation_evidence_must_match_the_resolved_model() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text='{"candidates":[]}')]),
        clock,
    )

    with pytest.raises(ConfigurationError, match="does not match"):
        ProviderAssistedCandidateExtractor(
            provider=provider,
            resolved_model=ResolvedModel(
                provider="fake",
                model="different-model",
                policy_name="fake",
                resolved_at=NOW,
            ),
            uow_factory=factory,
            clock=clock,
            ids=SequenceIdFactory(UUID(int=value) for value in range(9_200, 9_300)),
            principal=principal(),
            agent_id=AGENT_ID,
            agent_version="1.0.0",
            policy_profile="default",
            policy_version="default@test",
            evidence=_evidence(),
            fallback=DeterministicCandidateExtractor(),
        )

    with pytest.raises(ConfigurationError, match="does not match"):
        ProviderAssistedCandidateExtractor(
            provider=provider,
            resolved_model=ResolvedModel(
                provider="fake",
                model="scripted",
                policy_name="fake",
                resolved_at=NOW,
            ),
            uow_factory=factory,
            clock=clock,
            ids=SequenceIdFactory(UUID(int=value) for value in range(9_300, 9_400)),
            principal=principal(),
            agent_id=AGENT_ID,
            agent_version="1.0.0",
            policy_profile="default",
            policy_version="default@test",
            evidence=_evidence().model_copy(update={"policy_version": "different@version"}),
            fallback=DeterministicCandidateExtractor(),
        )


async def test_provider_proposals_still_pass_source_and_secret_gates(tmp_path: Path) -> None:
    evidence_path = tmp_path / "provider-memory-evidence.json"
    evidence_path.write_text(
        _evidence()
        .model_copy(
            update={
                "model_policy": "fake-balanced",
                "policy_version": _runtime_policy_version(),
            }
        )
        .model_dump_json(),
        encoding="utf-8",
    )
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=MemoryProviderExtractionMode.REQUIRED,
        memory_provider_extraction_evidence=evidence_path,
        artifact_root=tmp_path / "artifacts",
    )
    response = json.dumps(
        {
            "candidates": [
                {
                    "claim_kind": "relationship",
                    "subject": "daughter",
                    "value": None,
                    "context": None,
                    "quantity": 1,
                    "evidence_quote": "my daughter",
                    "polarity": "assert",
                    "source_event_ids": [999],
                    "model_confidence": 0.99,
                    "proposed_portability": "contextual",
                    "sensitivity_guess": "internal",
                    "valid_from": None,
                    "expires_hint": None,
                },
                {
                    "claim_kind": "hobby",
                    "subject": "hobby",
                    "value": "club service login",
                    "context": None,
                    "quantity": None,
                    "evidence_quote": "club service login",
                    "polarity": "assert",
                    "source_event_ids": [2],
                    "model_confidence": 0.99,
                    "proposed_portability": "contextual",
                    "sensitivity_guess": "restricted",
                    "valid_from": None,
                    "expires_hint": None,
                },
            ]
        }
    )

    async with build(
        settings=settings,
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        script=FakeModelScript(turns=[ScriptedTurn(text=response)]),
    ) as app:
        session_id = await app.sessions.create()
        async with app.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": "The club service login is api_token=placeholder."},
                )
            )
        result = await app.memory.run(
            trigger="session_idle",
            scope="general",
            session_id=session_id,
        )

    assert result.beliefs == []
    assert result.run.candidates_proposed == 1
    assert result.run.rejected == 1


async def test_composition_can_gather_evidence_without_activating_provider_extraction(
    tmp_path: Path,
) -> None:
    response = json.dumps(
        {
            "candidates": [
                {
                    "claim_kind": "relationship",
                    "subject": "daughter",
                    "value": None,
                    "context": None,
                    "quantity": 1,
                    "evidence_quote": "my daughter",
                    "polarity": "assert",
                    "source_event_ids": [2],
                    "model_confidence": 0.9,
                    "proposed_portability": "contextual",
                    "sensitivity_guess": "sensitive",
                    "valid_from": None,
                    "expires_hint": None,
                }
            ]
        }
    )
    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")

    async with build(
        settings=settings,
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        script=FakeModelScript(turns=[ScriptedTurn(text=response)]),
        memory_provider_evaluation_mode=True,
    ) as app:
        session_id = await app.sessions.create()
        async with app.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": "The astronomy club was my daughter's idea."},
                )
            )
        result = await app.memory.run(
            trigger="session_idle",
            scope="general",
            session_id=session_id,
        )
        async with app.uow_factory() as uow:
            audits = await uow.process_events.list("memory.provider_extraction.completed")

    assert [item.subject for item in result.beliefs] == ["daughter"]
    assert audits[0].payload["evaluation_mode"] is True


async def test_auto_mode_falls_back_and_records_why_evidence_did_not_match(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "provider-memory-evidence.json"
    evidence_path.write_text(
        _evidence().model_copy(update={"model_policy": "different-policy"}).model_dump_json(),
        encoding="utf-8",
    )
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=MemoryProviderExtractionMode.AUTO,
        memory_provider_extraction_evidence=evidence_path,
        artifact_root=tmp_path / "artifacts",
    )

    async with build(settings=settings, storage="memory") as app, app.uow_factory() as uow:
        selections = await uow.process_events.list("memory.provider_extraction.selection")

    assert len(selections) == 1
    assert selections[0].payload["mode"] == "auto"
    assert selections[0].payload["outcome"] == "deterministic_fallback"
    assert selections[0].payload["reason"] == "no_matching_evidence"
    assert ":no_matching_evidence:none:" in selections[0].derivation_key


async def test_auto_mode_activates_matching_release_bundled_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_root = tmp_path / "release-evidence"
    release_root.mkdir()
    (release_root / "fake-balanced.json").write_text(
        _evidence()
        .model_copy(
            update={
                "model_policy": "fake-balanced",
                "policy_version": _runtime_policy_version(),
            }
        )
        .model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        config_module,
        "PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT",
        release_root,
    )
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=MemoryProviderExtractionMode.AUTO,
        artifact_root=tmp_path / "artifacts",
    )

    async with build(settings=settings, storage="memory") as app, app.uow_factory() as uow:
        selections = await uow.process_events.list("memory.provider_extraction.selection")

    assert len(selections) == 1
    assert selections[0].payload["outcome"] == "activated"
    assert selections[0].payload["evidence_source"] == "release"
    assert ":matching_evidence:release:" in selections[0].derivation_key
    assert selections[0].payload["evidence_build_ref"] == "test-build"
    assert selections[0].payload["evidence_corpus_sha256"] == "a" * 64


async def test_off_mode_never_resolves_a_formation_provider(tmp_path: Path) -> None:
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=MemoryProviderExtractionMode.OFF,
        artifact_root=tmp_path / "artifacts",
    )

    async with (
        build(
            settings=settings,
            storage="memory",
            model_policy="balanced",
        ) as app,
        app.uow_factory() as uow,
    ):
        selections = await uow.process_events.list("memory.provider_extraction.selection")

    assert len(selections) == 1
    assert selections[0].payload["outcome"] == "disabled"
    assert selections[0].payload["provider"] is None
    assert selections[0].payload["model"] is None


async def test_auto_mode_falls_back_when_no_extraction_model_can_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_resolution(*_args: object, **_kwargs: object) -> object:
        raise ConfigurationError("model lacks structured output")

    monkeypatch.setattr(
        "agent_core.bootstrap.StaticModelRouter.resolve",
        fail_resolution,
    )
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=MemoryProviderExtractionMode.AUTO,
        artifact_root=tmp_path / "artifacts",
    )

    async with (
        build(
            settings=settings,
            storage="memory",
            model_policy="balanced",
        ) as app,
        app.uow_factory() as uow,
    ):
        selections = await uow.process_events.list("memory.provider_extraction.selection")

    assert len(selections) == 1
    assert selections[0].payload["outcome"] == "deterministic_fallback"
    assert selections[0].payload["reason"] == "model_resolution_failed"


async def test_auto_mode_reports_a_missing_provider_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ("VEETBOT_OPENAI_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=MemoryProviderExtractionMode.AUTO,
        artifact_root=tmp_path / "artifacts",
    )

    async with (
        build(
            settings=settings,
            storage="memory",
            model_policy="balanced",
        ) as app,
        app.uow_factory() as uow,
    ):
        selections = await uow.process_events.list("memory.provider_extraction.selection")

    assert len(selections) == 1
    assert selections[0].payload["outcome"] == "deterministic_fallback"
    assert selections[0].payload["reason"] == "provider_credential_unavailable"


async def test_evaluation_mode_cannot_bypass_an_active_evidence_gate(tmp_path: Path) -> None:
    evidence_path = tmp_path / "provider-memory-evidence.json"
    evidence_path.write_text(
        _evidence()
        .model_copy(
            update={
                "model_policy": "fake-balanced",
                "policy_version": _runtime_policy_version(),
            }
        )
        .model_dump_json(),
        encoding="utf-8",
    )
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=MemoryProviderExtractionMode.REQUIRED,
        memory_provider_extraction_evidence=evidence_path,
    )

    with pytest.raises(ConfigurationError, match="mutually exclusive"):
        async with build(
            settings=settings,
            memory_provider_evaluation_mode=True,
        ):
            pass
