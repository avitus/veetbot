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
from agent_core.domain.memory import ProviderExtractionEvaluationEvidence
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
from agent_core.memory.provider_extraction import ProviderAssistedCandidateExtractor
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
        extractor_version="provider-assisted-v1",
        formation_policy_version="formation@3",
        model_policy="fake",
        provider="fake",
        model="scripted",
        policy_profile="default",
        policy_version="default@test",
        build_ref="test-build",
        corpus_sha256="a" * 64,
        sample_count=20,
        deterministic_supported_candidates=10,
        provider_supported_candidates=15,
        fabricated_candidates=0,
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


def test_evaluation_evidence_binds_the_compiled_policy_version() -> None:
    evidence = _evidence().model_dump()
    evidence["policy_version"] = "default@profile+hline"

    parsed = ProviderExtractionEvaluationEvidence.model_validate(evidence)

    assert parsed.policy_version == "default@profile+hline"


async def test_provider_extractor_uses_bounded_structured_call_and_audits_usage() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "The astronomy club was my daughter's idea.")
    response = json.dumps(
        {
            "candidates": [
                {
                    "belief_type": "relationship",
                    "subject": "daughter",
                    "statement": "User has at least one daughter.",
                    "polarity": "assert",
                    "source_event_ids": [source],
                    "model_confidence": 0.92,
                    "proposed_scope": "project-a",
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
    candidate_schema = schema["$defs"]["_ModelCandidate"]
    assert set(candidate_schema["required"]) == set(candidate_schema["properties"])
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
    assert "astronomy club" not in json.dumps(payload)
    assert "at least one daughter" not in json.dumps(payload)


async def test_provider_extractor_rejects_ungrounded_named_claim_with_valid_source() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "The astronomy club was my daughter's idea.")
    response = json.dumps(
        {
            "candidates": [
                {
                    "belief_type": "fact",
                    "subject": "astronomy club",
                    "statement": "User's astronomy club is in Paris.",
                    "polarity": "assert",
                    "source_event_ids": [source],
                    "model_confidence": 0.95,
                    "proposed_scope": "project-a",
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

    assert candidates == []


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
                    "belief_type": "relationship",
                    "subject": "daughter",
                    "statement": "User has at least one daughter.",
                    "polarity": "assert",
                    "source_event_ids": [2],
                    "model_confidence": 0.9,
                    "proposed_scope": "general",
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
    assert result.run.model.startswith("provider-assisted-v1:fake:scripted")
    assert result.run.policy_version == "formation@3"


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

    assert (
        await extractor.extract(
            await session_events(factory),
            principal=principal(),
            scope="project-a",
        )
        == []
    )
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
    assert audits[0].payload["outcome"] == "failed"
    assert audits[0].payload["error_class"] == "ModelStreamError"


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


async def test_cancellation_is_audited_and_propagated() -> None:
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
    task = asyncio.create_task(
        extractor.extract(
            await session_events(factory),
            principal=principal(),
            scope="project-a",
        )
    )
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

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
                    "belief_type": "fact",
                    "subject": "fabricated",
                    "statement": "User owns a private island.",
                    "polarity": "assert",
                    "source_event_ids": [999],
                    "model_confidence": 0.99,
                    "proposed_scope": "general",
                    "proposed_portability": "contextual",
                    "sensitivity_guess": "internal",
                    "valid_from": None,
                    "expires_hint": None,
                },
                {
                    "belief_type": "fact",
                    "subject": "credential",
                    "statement": "User API key: secret=do-not-store.",
                    "polarity": "assert",
                    "source_event_ids": [2],
                    "model_confidence": 0.99,
                    "proposed_scope": "general",
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
                    payload={"content": "The words API credential appear in this discussion."},
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
                    "belief_type": "relationship",
                    "subject": "daughter",
                    "statement": "User has at least one daughter.",
                    "polarity": "assert",
                    "source_event_ids": [2],
                    "model_confidence": 0.9,
                    "proposed_scope": "general",
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


async def test_auto_mode_reports_a_missing_provider_credential(tmp_path: Path) -> None:
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
