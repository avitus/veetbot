"""Memory candidate extractor contract: bounded, structured, trusted proposals."""

from __future__ import annotations

from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
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
from agent_core.memory.formation import MAX_EXTRACTOR_PROPOSALS, DeterministicCandidateExtractor
from agent_core.memory.model_extraction import ModelAssistedCandidateExtractor
from agent_core.memory.provider_extraction import ProviderAssistedCandidateExtractor
from agent_core.ports.memory import MemoryCandidateExtractor
from tests.contract.memory_fixtures import formation_stack, session_events, user_event
from tests.contract.support import AGENT_ID, NOW, principal

COVERED_IMPLEMENTATIONS = frozenset(
    {
        "agent_core.memory.formation.DeterministicCandidateExtractor",
        "agent_core.memory.model_extraction.ModelAssistedCandidateExtractor",
        "agent_core.memory.provider_extraction.ProviderAssistedCandidateExtractor",
    }
)


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
        extractor_version="provider-assisted-v1",
        formation_policy_version="formation@3",
        model_policy="fake",
        provider="fake",
        model="scripted",
        policy_profile="default",
        policy_version="default@test",
        build_ref="contract-test",
        corpus_sha256="a" * 64,
        sample_count=20,
        deterministic_supported_candidates=10,
        provider_supported_candidates=11,
        fabricated_candidates=0,
        deterministic_policy_failures=0,
        provider_policy_failures=0,
        evaluated_at=NOW,
    )


async def _extractor_subject(
    implementation: str,
) -> tuple[MemoryCandidateExtractor, MemoryUnitOfWorkFactory]:
    clock, factory, _service, _retriever = await formation_stack()
    if implementation.endswith("DeterministicCandidateExtractor"):
        return DeterministicCandidateExtractor(), factory

    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="not-json")]),
        clock,
    )
    if implementation.endswith("ModelAssistedCandidateExtractor"):
        return (
            ModelAssistedCandidateExtractor(
                router=_StructuredRouter(),
                providers={"fake": provider},
                clock=clock,
                ids=SequenceIdFactory([UUID(int=8_001)]),
                model_policy="fake",
            ),
            factory,
        )
    if implementation.endswith("ProviderAssistedCandidateExtractor"):
        return (
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
                ids=SequenceIdFactory(UUID(int=value) for value in range(8_100, 8_200)),
                principal=principal(),
                agent_id=AGENT_ID,
                agent_version="1.0.0",
                policy_profile="default",
                policy_version="default@test",
                evidence=_evidence(),
                fallback=DeterministicCandidateExtractor(),
            ),
            factory,
        )
    raise AssertionError(f"unregistered extractor contract subject: {implementation}")


def _assert_candidate_extractor_contract_covers_every_shipped_implementation() -> None:
    assert {
        "agent_core.memory.formation.DeterministicCandidateExtractor",
        "agent_core.memory.model_extraction.ModelAssistedCandidateExtractor",
        "agent_core.memory.provider_extraction.ProviderAssistedCandidateExtractor",
    } == COVERED_IMPLEMENTATIONS


def test_candidate_extractor_contract_covers_every_shipped_implementation() -> None:
    _assert_candidate_extractor_contract_covers_every_shipped_implementation()


@pytest.mark.parametrize("implementation", sorted(COVERED_IMPLEMENTATIONS))
async def test_extractor_returns_separate_provenance_bound_candidate_proposals(
    implementation: str,
) -> None:
    _assert_candidate_extractor_contract_covers_every_shipped_implementation()
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
