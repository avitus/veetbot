"""Rich memory extraction remains grounded, bounded, audited, and recoverable."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryCandidate,
    Polarity,
    Portability,
    Sensitivity,
)
from agent_core.domain.messages import (
    CapabilitySet,
    FakeModelScript,
    ModelCapabilities,
    ModelUsage,
    ProviderPin,
    ResolvedModel,
    ScriptedTurn,
)
from agent_core.memory.model_extraction import (
    MemoryExtractionAudit,
    ModelAssistedCandidateExtractor,
)
from tests.contract.support import NOW, PRINCIPAL_ID, RUN_ID, SESSION_ID, principal
from tests.integration.m2_support import memory_settings


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
            provider="openai",
            model="memory-model",
            policy_name=model_policy,
            capabilities=ModelCapabilities(structured_output=True),
            resolved_at=NOW,
        )

    async def resolve_pinned(self, pin: ProviderPin) -> ResolvedModel:
        raise AssertionError(f"unexpected pin: {pin}")

    def pin(self, run_id: UUID, resolved: ResolvedModel) -> ProviderPin:
        raise AssertionError(f"unexpected pin: {run_id} {resolved}")


def _event(text: str, *, sequence: int = 7) -> EventEnvelope:
    return EventEnvelope(
        id=sequence,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        sequence=sequence,
        event_type="user.message.created",
        payload_schema_version=1,
        actor_type="principal",
        actor_id=PRINCIPAL_ID,
        payload={"content": text},
        trace_id=None,
        created_at=NOW,
    )


def _candidate(*, name: str = "Riv", source_event_id: int = 7) -> dict[str, object]:
    return {
        "belief_type": BeliefType.RELATIONSHIP.value,
        "subject": "daughter",
        "statement": f"User's daughter is {name}.",
        "polarity": "assert",
        "source_event_ids": [source_event_id],
        "model_confidence": 0.96,
        "proposed_scope": "general",
        "proposed_portability": Portability.CONTEXTUAL.value,
        "sensitivity_guess": Sensitivity.SENSITIVE.value,
        "valid_from": None,
        "expires_hint": None,
    }


async def _extractor(
    text: str,
    *,
    usage: ModelUsage | None = None,
) -> tuple[
    ModelAssistedCandidateExtractor,
    FakeModelProvider,
    list[MemoryExtractionAudit],
]:
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=text,
                    usage=usage
                    or ModelUsage(
                        input_tokens=120,
                        output_tokens=40,
                        provider="openai",
                        model="memory-model",
                    ),
                )
            ]
        ),
        FixedClock(NOW),
    )
    audits: list[MemoryExtractionAudit] = []

    async def record(audit: MemoryExtractionAudit) -> None:
        audits.append(audit)

    extractor = ModelAssistedCandidateExtractor(
        router=_StructuredRouter(),
        providers={"openai": provider},
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([UUID(int=901)]),
        model_policy="balanced",
        audit=record,
    )
    return extractor, provider, audits


async def test_model_assisted_extractor_adds_a_grounded_relationship_candidate() -> None:
    extractor, provider, audits = await _extractor(json.dumps({"candidates": [_candidate()]}))

    candidates = await extractor.extract(
        [_event("Riv is the name of my daughter.")],
        principal=principal(),
        scope="general",
    )

    assert [(item.subject, item.statement) for item in candidates] == [
        ("daughter", "User has at least one daughter."),
        ("daughter", "User's daughter is Riv."),
    ]
    assert len(provider.requests) == 1
    schema = provider.requests[0].response_schema
    assert schema is not None
    candidate_schema = schema["$defs"]["_ModelCandidate"]
    assert set(candidate_schema["required"]) == set(candidate_schema["properties"])
    assert provider.requests[0].tools == []
    assert provider.requests[0].maximum_output_tokens == 4096
    assert audits[0].usage.input_tokens == 120
    assert audits[0].fallback_used is False


async def test_model_assisted_extractor_falls_back_after_malformed_output() -> None:
    extractor, _provider, audits = await _extractor("not json")

    candidates = await extractor.extract(
        [_event("My wife is Morgan.")],
        principal=principal(),
        scope="general",
    )

    assert [(item.subject, item.statement) for item in candidates] == [
        ("wife", "User's wife is Morgan.")
    ]
    assert audits[0].fallback_used is True
    assert audits[0].error_class == "ValidationError"


async def test_model_assisted_extractor_rejects_an_ungrounded_named_entity() -> None:
    extractor, _provider, audits = await _extractor(
        json.dumps({"candidates": [_candidate(name="Alice")]})
    )

    candidates = await extractor.extract(
        [_event("Riv is the name of my daughter.")],
        principal=principal(),
        scope="general",
    )

    assert [(item.subject, item.statement) for item in candidates] == [
        ("daughter", "User has at least one daughter.")
    ]
    assert audits[0].candidates_returned == 1
    assert audits[0].fallback_used is False


def test_model_assisted_extractor_accepts_a_grounded_decimal() -> None:
    candidate = MemoryCandidate(
        belief_type=BeliefType.FACT,
        subject="telescopes",
        statement="User has 3.5 telescopes.",
        source_event_ids=[7],
        model_confidence=0.96,
        proposed_scope="general",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
    )

    assert ModelAssistedCandidateExtractor._is_grounded(
        candidate,
        [_event("I have 3.5 telescopes.")],
        principal=principal(),
        scope="general",
    )


def test_model_assisted_extractor_preserves_distinct_polarity_and_belief_type() -> None:
    asserted = MemoryCandidate(
        belief_type=BeliefType.RELATIONSHIP,
        subject="daughter",
        statement="User has at least one daughter.",
        polarity=Polarity.ASSERT,
        source_event_ids=[7],
        model_confidence=0.96,
        proposed_scope="general",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
    )
    retracted = asserted.model_copy(update={"polarity": Polarity.RETRACT})
    factual = asserted.model_copy(update={"belief_type": BeliefType.FACT})

    assert ModelAssistedCandidateExtractor._deduplicate([asserted, retracted, factual]) == [
        asserted,
        retracted,
        factual,
    ]


async def test_model_assisted_extractor_rejects_output_over_its_dedicated_budget() -> None:
    extractor, _provider, audits = await _extractor(
        json.dumps({"candidates": [_candidate()]}),
        usage=ModelUsage(
            input_tokens=120,
            output_tokens=4_097,
            provider="openai",
            model="memory-model",
        ),
    )

    candidates = await extractor.extract(
        [_event("My wife is Morgan.")],
        principal=principal(),
        scope="general",
    )

    assert [(item.subject, item.statement) for item in candidates] == [
        ("wife", "User's wife is Morgan.")
    ]
    assert audits[0].fallback_used is True
    assert audits[0].error_class == "MemoryExtractionBudgetError"


async def test_routed_composition_does_not_activate_rich_extraction_without_evidence(
    tmp_path: Path,
) -> None:
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=json.dumps({"candidates": [_candidate(source_event_id=2)]}),
                    usage=ModelUsage(
                        input_tokens=80,
                        output_tokens=30,
                        provider="openai",
                        model="memory-model",
                    ),
                )
            ]
        ),
        FixedClock(NOW),
    )
    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")

    async with build(
        settings=settings,
        storage="memory",
        model_policy="balanced",
        model_provider_overrides={"openai": provider},
        clock=FixedClock(NOW),
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
                    payload={"content": "Riv is the name of my daughter."},
                )
            )
        result = await app.memory.run(
            trigger="session_close",
            scope="general",
            session_id=session_id,
        )
        async with app.uow_factory() as uow:
            audits = await uow.process_events.list("memory.extraction.completed")
            selections = await uow.process_events.list("memory.provider_extraction.selection")

    assert [(item.subject, item.statement) for item in result.beliefs] == [
        ("daughter", "User has at least one daughter.")
    ]
    assert result.run.policy_version == "formation@2"
    assert provider.requests == []
    assert audits == []
    assert len(selections) == 1
    assert selections[0].payload["outcome"] == "deterministic_fallback"
    assert selections[0].payload["reason"] == "no_matching_evidence"
    assert source.sequence == 2
