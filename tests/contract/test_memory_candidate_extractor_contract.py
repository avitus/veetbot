"""Memory candidate extractor contract: bounded, structured, trusted proposals."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.memory import ProviderExtractionEvaluationEvidence
from agent_core.domain.messages import (
    CapabilitySet,
    FakeModelScript,
    ModelCapabilities,
    ProviderPin,
    ResolvedModel,
    ScriptedTurn,
)
from agent_core.memory import SHIPPED_MEMORY_CANDIDATE_EXTRACTORS
from agent_core.memory.distillation import NemoriAssistedCandidateExtractor
from agent_core.memory.formation import MAX_EXTRACTOR_PROPOSALS, DeterministicCandidateExtractor
from agent_core.memory.model_extraction import ModelAssistedCandidateExtractor
from agent_core.memory.provider_extraction import (
    PROVIDER_FORMATION_POLICY_VERSION,
    ProviderAssistedCandidateExtractor,
)
from agent_core.ports.memory import MemoryCandidateExtractor
from tests.contract.memory_fixtures import formation_stack, session_events, user_event
from tests.contract.support import AGENT_ID, NOW, principal


class _StructuredRouter:
    async def resolve(
        self,
        model_policy: str,
        *,
        tenant_id: str,
        required: CapabilitySet | None = None,
    ) -> ResolvedModel:
        del tenant_id, required
        return ResolvedModel(
            provider="fake",
            model="scripted",
            policy_name=model_policy,
            capabilities=ModelCapabilities(structured_output=True),
            resolved_at=NOW,
        )

    async def resolve_pinned(self, pin: ProviderPin) -> ResolvedModel:
        raise AssertionError(f"unexpected pin: {pin}")

    def pin(self, run_id: UUID, resolved: ResolvedModel) -> ProviderPin:
        raise AssertionError(f"unexpected pin: {run_id} {resolved}")


def _evidence() -> ProviderExtractionEvaluationEvidence:
    return ProviderExtractionEvaluationEvidence(
        extractor_version="provider-assisted-v2",
        formation_policy_version=PROVIDER_FORMATION_POLICY_VERSION,
        model_policy="fake",
        provider="fake",
        model="scripted",
        policy_profile="default",
        policy_version="default@test",
        build_ref="contract-test",
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


def _unparseable_turns(implementation: type[object]) -> list[ScriptedTurn]:
    """One unusable model reply per provider stage, so every subject takes its fallback."""

    stages = 3 if implementation is NemoriAssistedCandidateExtractor else 1
    return [ScriptedTurn(text="not-json") for _stage in range(stages)]


async def _extractor_subject(
    implementation: type[object],
) -> tuple[MemoryCandidateExtractor, MemoryUnitOfWorkFactory]:
    clock, factory, _service, _retriever = await formation_stack()
    extractor = _build_extractor(
        implementation, clock=clock, factory=factory, turns=_unparseable_turns(implementation)
    )
    return extractor, factory


def _build_extractor(
    implementation: type[object],
    *,
    clock: FixedClock,
    factory: MemoryUnitOfWorkFactory,
    turns: list[ScriptedTurn],
) -> MemoryCandidateExtractor:
    if implementation is DeterministicCandidateExtractor:
        return DeterministicCandidateExtractor()

    provider = FakeModelProvider(FakeModelScript(turns=turns), clock)
    if implementation is ModelAssistedCandidateExtractor:
        return ModelAssistedCandidateExtractor(
            router=_StructuredRouter(),
            providers={"fake": provider},
            clock=clock,
            ids=SequenceIdFactory([UUID(int=8_001)]),
            model_policy="fake",
        )
    if implementation is ProviderAssistedCandidateExtractor:
        return ProviderAssistedCandidateExtractor(
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
    if implementation is NemoriAssistedCandidateExtractor:
        return NemoriAssistedCandidateExtractor(
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
        )
    raise AssertionError(
        f"unregistered extractor contract subject: "
        f"{implementation.__module__}.{implementation.__qualname__}"
    )


@pytest.mark.parametrize(
    "implementation",
    SHIPPED_MEMORY_CANDIDATE_EXTRACTORS,
    ids=lambda implementation: implementation.__name__,
)
async def test_extractor_returns_separate_provenance_bound_candidate_proposals(
    implementation: type[object],
) -> None:
    extractor, factory = await _extractor_subject(implementation)
    source = await user_event(factory, "I have an Apple Watch and a BMW X3.")
    events = await session_events(factory)

    candidates = await extractor.extract(
        events,
        principal=principal(),
        scope="project-a",
    )

    assert {candidate.subject for candidate in candidates} == {"Apple Watch", "BMW X3"}
    assert all(candidate.source_event_ids == [source] for candidate in candidates)
    assert all(candidate.proposed_scope == "project-a" for candidate in candidates)
    assert all(candidate.model_confidence > 0 for candidate in candidates)


async def test_extractor_enforces_its_candidate_volume_cap() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "I have an Apple Watch and a BMW X3.")

    candidates = await DeterministicCandidateExtractor(maximum_candidates=1).extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert len(candidates) == 1

    with pytest.raises(ValueError, match="must not exceed"):
        DeterministicCandidateExtractor(maximum_candidates=MAX_EXTRACTOR_PROPOSALS + 1)

    with pytest.raises(ValueError, match="must be positive"):
        DeterministicCandidateExtractor(maximum_candidates=0)


def _over_cap_turns(implementation: type[object], source: int) -> list[ScriptedTurn]:
    """Script one schema-shaped reply proposing one candidate more than the ceiling."""

    count = MAX_EXTRACTOR_PROPOSALS + 1
    candidates: list[dict[str, object]]
    if implementation is ModelAssistedCandidateExtractor:
        candidates = [
            {
                "belief_type": "fact",
                "subject": f"Proposal {index}",
                "statement": f"User owns proposal {index}",
                "polarity": "assert",
                "source_event_ids": [source],
                "model_confidence": 0.9,
                "proposed_scope": "project-a",
                "proposed_portability": "contextual",
                "sensitivity_guess": "internal",
                "valid_from": None,
                "expires_hint": None,
            }
            for index in range(count)
        ]
        return [ScriptedTurn(text=json.dumps({"candidates": candidates}))]
    if implementation is ProviderAssistedCandidateExtractor:
        candidates = [
            {
                "claim_kind": "project_fact",
                "subject": f"proposal {index}",
                "value": None,
                "context": None,
                "quantity": None,
                "evidence_quote": "Apple Watch",
                "polarity": "assert",
                "source_event_ids": [source],
                "model_confidence": 0.9,
                "proposed_portability": "contextual",
                "sensitivity_guess": "internal",
                "valid_from": None,
                "expires_hint": None,
            }
            for index in range(count)
        ]
        return [ScriptedTurn(text=json.dumps({"candidates": candidates}))]
    if implementation is NemoriAssistedCandidateExtractor:
        candidates = [
            {
                "subject": f"Proposal {index}",
                "statement": f"User owns proposal {index}",
                "source_event_ids": [source],
                "sensitivity_guess": "internal",
                "claim_kind": "resource",
                "derivation": "direct",
                "polarity": "assert",
                "evidence_spans": [{"source_event_id": source, "text": "Apple Watch"}],
            }
            for index in range(count)
        ]
        # The coverage row cites one candidate so that the candidate list is the
        # only element over the ceiling.
        distillation: dict[str, object] = {
            "candidates": candidates,
            "coverage": [
                {
                    "coverage_unit_id": f"{source}:1",
                    "decision": "formed",
                    "candidate_indexes": [0],
                    "prediction_indexes": [],
                }
            ],
        }
        # Episode integration and anticipation take their local fallbacks; the
        # distillation stage is the one that proposes candidates.
        return [
            ScriptedTurn(text="not-json"),
            ScriptedTurn(text="not-json"),
            ScriptedTurn(text=json.dumps(distillation)),
        ]
    raise AssertionError(
        f"unregistered extractor contract subject: "
        f"{implementation.__module__}.{implementation.__qualname__}"
    )


@pytest.mark.parametrize(
    "implementation",
    [
        implementation
        for implementation in SHIPPED_MEMORY_CANDIDATE_EXTRACTORS
        if implementation is not DeterministicCandidateExtractor
    ],
    ids=lambda implementation: implementation.__name__,
)
async def test_model_backed_extractor_never_returns_more_than_the_proposal_ceiling(
    implementation: type[object],
) -> None:
    """A model reply proposing more than MAX_EXTRACTOR_PROPOSALS yields at most the cap."""

    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "I have an Apple Watch and a BMW X3.")
    extractor = _build_extractor(
        implementation,
        clock=clock,
        factory=factory,
        turns=_over_cap_turns(implementation, source),
    )

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert len(candidates) <= MAX_EXTRACTOR_PROPOSALS
    assert all(candidate.source_event_ids == [source] for candidate in candidates)
