"""Milestone 21 adaptive memory-distillation gates."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

import agent_core.config as config_module
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.persistence.memory_repositories import _memory_values
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.bootstrap import build
from agent_core.config import (
    MemoryFormationPolicyPin,
    MemoryProviderExtractionMode,
    load_config_document,
)
from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryCandidate,
    MemoryClaimKind,
    MemoryDerivation,
    MemoryDistillationEvidence,
    MemoryEdit,
    MemoryLongevity,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    ProviderExtractionEvaluationEvidence,
    RejectionKind,
    Sensitivity,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ModelLimits,
    ModelPricing,
    ModelTransientError,
    ModelUsage,
    ResolvedModel,
    ScriptedTurn,
    SystemMessage,
    TextPart,
    UserMessage,
)
from agent_core.domain.views import MemoryView
from agent_core.evals.memory_distillation import (
    DistillationEvaluationBelief,
    MemoryDistillationCase,
    load_distillation_corpus,
    score_distillation_case,
)
from agent_core.memory import SHIPPED_MEMORY_CANDIDATE_EXTRACTORS, formation
from agent_core.memory.distillation import (
    DISTILLATION_MAXIMUM_OUTPUT_TOKENS,
    MAX_SEGMENT_COVERAGE_UNITS,
    NemoriAssistedCandidateExtractor,
    _candidates_semantically_duplicate,
    _normalize_provider_candidate,
    deterministic_integrated_episode,
    plan_segments,
    select_distillation_policy,
    validate_integrated_episode,
)
from agent_core.memory.equivalence import (
    statement_supports_clause,
    statements_equivalent,
    subject_matches,
)
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.model_extraction import ModelAssistedCandidateExtractor
from agent_core.memory.provider_extraction import (
    PROVIDER_FORMATION_POLICY_VERSION,
    REPAIRED_PROVIDER_EXTRACTOR_VERSION,
    REPAIRED_PROVIDER_FORMATION_POLICY_VERSION,
    ProviderAssistedCandidateExtractor,
)
from agent_core.memory.retrieval import _score, render_memory
from agent_core.policy.loader import load_ruleset_documents
from tests.contract.memory_fixtures import (
    formation_stack,
    memory,
    recall_query,
    session_events,
    user_event,
)
from tests.contract.support import AGENT_ID, NOW, PRINCIPAL_ID, SESSION_ID, TENANT, principal
from tests.integration.m2_support import memory_settings


def _candidate(**overrides: object) -> MemoryCandidate:
    values: dict[str, object] = {
        "belief_type": "user_model_attr",
        "subject": "personal AI agent",
        "statement": "User is building a personal AI agent.",
        "source_event_ids": [7],
        "model_confidence": 0.65,
        "proposed_scope": "general",
        "proposed_portability": "portable",
        "sensitivity_guess": "internal",
        "valid_from": datetime(2026, 8, 31, tzinfo=UTC),
        "claim_kind": "ongoing_project",
        "derivation": "direct",
        "longevity": "ongoing",
        "evidence_spans": [{"source_event_id": 7, "text": "building a personal AI agent"}],
    }
    values.update(overrides)
    return MemoryCandidate.model_validate(values)


def test_completed_policy_controls_are_frozen() -> None:
    assert formation.FORMATION_POLICY_VERSION == "formation@7"
    assert PROVIDER_FORMATION_POLICY_VERSION == "formation@8"
    assert NemoriAssistedCandidateExtractor.name == "nemori-assisted-v1"
    assert (
        formation.DeterministicCandidateExtractor,
        ModelAssistedCandidateExtractor,
        ProviderAssistedCandidateExtractor,
        NemoriAssistedCandidateExtractor,
    ) == SHIPPED_MEMORY_CANDIDATE_EXTRACTORS


def test_candidate_language_is_closed_and_complete() -> None:
    direct = _candidate()
    assert direct.claim_kind == "ongoing_project"
    assert direct.derivation == "direct"
    assert direct.longevity == "ongoing"
    assert direct.evidence_spans[0].source_event_id == 7

    hypotheses = _candidate(
        claim_kind="skill",
        derivation="hypothesis",
        longevity="tentative",
        statement="User likely has software-development experience.",
        model_confidence=0.35,
    )
    assert hypotheses.derivation == "hypothesis"

    for field, value in (
        ("claim_kind", "biography_guess"),
        ("derivation", "certain"),
        ("longevity", "forever"),
    ):
        with pytest.raises(ValidationError):
            _candidate(**{field: value})

    with pytest.raises(ValidationError):
        _candidate(evidence_spans=[])

    with pytest.raises(ValidationError):
        _candidate(evidence_spans=[{"source_event_id": 8, "text": "building"}])


def _personal_agent_event() -> EventEnvelope:
    return EventEnvelope(
        id=7,
        session_id=SESSION_ID,
        run_id=None,
        sequence=7,
        event_type="user.message.created",
        payload_schema_version=1,
        actor_type="principal",
        actor_id=PRINCIPAL_ID,
        payload={
            "content": (
                "I am building a personal AI agent and I'm wondering what to use "
                "for web search and web fetch."
            )
        },
        trace_id=None,
        created_at=NOW,
    )


async def _personal_agent_candidates() -> list[MemoryCandidate]:
    extractor = formation.HighRecallCandidateExtractor()
    return await extractor.extract(
        [_personal_agent_event()], principal=principal(), scope="general"
    )


async def test_personal_agent_project_forms_as_direct_ongoing_memory() -> None:
    candidates = await _personal_agent_candidates()
    project = next(
        candidate
        for candidate in candidates
        if candidate.statement == "User is building a personal AI agent."
    )

    assert project.subject == "personal AI agent"
    assert project.claim_kind == "ongoing_project"
    assert project.derivation == "direct"
    assert project.longevity == "ongoing"
    assert project.model_confidence == 0.65
    assert project.source_event_ids == [7]
    assert project.evidence_spans[0].text == "building a personal AI agent"


async def test_ongoing_project_evidence_preserves_exact_source_whitespace() -> None:
    event = _personal_agent_event().model_copy(
        update={"payload": {"content": "I am building  a personal AI agent."}}
    )

    candidates = await formation.HighRecallCandidateExtractor().extract(
        [event], principal=principal(), scope="general"
    )
    project = next(
        candidate for candidate in candidates if candidate.claim_kind == "ongoing_project"
    )

    assert project.evidence_spans[0].text == "building  a personal AI agent"
    assert project.evidence_spans[0].text in event.payload["content"]


async def test_rich_training_conversation_forms_atomic_high_recall_memories() -> None:
    """The production training conversation forms all eleven labeled memories.

    The gate scores the deterministic fallback with the same strict scorer the
    comparative evaluation uses, so a rendering that only a lenient matcher
    would accept, a generic subject, or a fabricated extra claim fails here
    before it can inflate published evidence.
    """

    root = Path(__file__).resolve().parents[2]
    corpus, _digest = load_distillation_corpus(root)
    case = next(case for case in corpus.cases if case.id == "rich-conversation-001")
    events = [
        _personal_agent_event().model_copy(
            update={"id": sequence, "sequence": sequence, "payload": {"content": event.text}}
        )
        for sequence, event in enumerate(case.events, start=7)
    ]

    candidates = await formation.HighRecallCandidateExtractor().extract(
        events,
        principal=principal(),
        scope="general",
    )
    beliefs = [
        DistillationEvaluationBelief(
            claim_kind=candidate.claim_kind,
            derivation=candidate.derivation,
            longevity=candidate.longevity,
            subject=candidate.subject,
            statement=candidate.statement,
        )
        for candidate in candidates
    ]
    score = score_distillation_case(case, beliefs)

    assert score.expected == 11
    assert score.matched == 11, [candidate.statement for candidate in candidates]
    assert score.false_positives == 0, [candidate.statement for candidate in candidates]
    for candidate in candidates:
        assert all(
            span.text in events[span.source_event_id - 7].payload["content"]
            for span in candidate.evidence_spans
        )
        assert candidate.subject.casefold() not in {"user", "the user"}
    assert not any(
        candidate.statement.startswith("User has a trained regularly") for candidate in candidates
    )


async def test_present_perfect_repair_does_not_drop_real_possessions() -> None:
    event = _personal_agent_event().model_copy(
        update={
            "payload": {"content": "I have trained regularly most of my life and I have a bike."}
        }
    )

    candidates = await formation.HighRecallCandidateExtractor().extract(
        [event], principal=principal(), scope="general"
    )

    assert any(candidate.statement == "User has a bike." for candidate in candidates)
    assert not any(
        candidate.statement == "User has a trained regularly most of my life."
        for candidate in candidates
    )


async def test_explicit_experience_is_repaired_as_a_skill_without_an_article() -> None:
    event = _personal_agent_event().model_copy(
        update={"payload": {"content": "I have ten years of Python development experience."}}
    )

    candidates = await formation.HighRecallCandidateExtractor().extract(
        [event], principal=principal(), scope="general"
    )

    assert any(
        candidate.claim_kind is MemoryClaimKind.SKILL
        and candidate.subject == "Python development"
        and candidate.statement == "User has ten years of Python development experience."
        for candidate in candidates
    )
    assert not any("has a ten years" in candidate.statement for candidate in candidates)


@pytest.mark.parametrize(
    ("message", "retraction"),
    [
        ("I do not use Redis anymore.", "User no longer uses a Redis."),
        ("I don't drive my BMW anymore.", "User no longer drives a BMW."),
        ("I no longer take meetings on Fridays.", "User no longer takes meetings on Fridays."),
        ("I stopped attending yoga.", "User no longer attends yoga."),
        ("I gave up swimming.", "User no longer swims."),
        ("I quit smoking.", "User no longer smokes."),
        ("That old memory saying I live in Rome is wrong.", None),
    ],
)
async def test_high_recall_fallback_forms_corrections_only_as_retractions(
    message: str, retraction: str | None
) -> None:
    """A correction may update an existing belief but never create one.

    formation@7 recognized "I don't drive my BMW anymore" as a retraction while
    the formation@9 fallback dropped it, so a provider outage silently left the
    old belief live after the watermark advanced. The fallback now emits the
    retraction, and only the retraction: the same turn's "my BMW" must not
    become a fresh possession.
    """

    event = _personal_agent_event().model_copy(update={"payload": {"content": message}})

    candidates = await formation.HighRecallCandidateExtractor().extract(
        [event], principal=principal(), scope="general"
    )

    assert formation.contains_automatic_memory_correction(message)
    if retraction is None:
        assert candidates == []
        return
    assert [(candidate.statement, candidate.polarity) for candidate in candidates] == [
        (retraction, Polarity.RETRACT)
    ]
    assert all(span.text in message for span in candidates[0].evidence_spans)


async def test_fallback_retraction_supersedes_the_belief_it_corrects() -> None:
    """Under formation@9 a retraction updates its subject and never creates memory."""

    clock, factory, baseline, _retriever = await formation_stack()
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=formation.HighRecallCandidateExtractor(),
        policy_version="formation@9",
    )
    await user_event(factory, "I drive a BMW.")
    first = await service.run(trigger="session_closed", scope="general", session_id=SESSION_ID)
    assert [belief.statement for belief in first.beliefs] == ["User drives a BMW."]

    clock.advance(timedelta(days=1))
    await user_event(factory, "I don't drive my BMW anymore. I gave up smoking.")
    second = await service.run(trigger="session_closed", scope="general", session_id=SESSION_ID)

    live = await service.list_memories()
    assert "User drives a BMW." not in {belief.statement for belief in live}
    assert second.run.superseded == 1
    assert second.run.decision_counts["superseded"] == 1
    # Nothing ever said the user smoked, so the retraction has nothing to update
    # and must not become a belief of its own.
    assert second.run.decision_counts["skipped_unmatched_retraction"] == 1
    assert sum(second.run.decision_counts.values()) == second.run.candidates_proposed
    assert not any("smok" in belief.statement for belief in live)


def test_provider_candidates_carry_polarity_and_corrections_pass_only_as_retractions() -> None:
    """The provider can name a correction, and only a retraction may cite one."""

    from agent_core.memory.distillation import _DistilledCandidate, _normalize_distilled_candidate

    schema = _DistilledCandidate.model_json_schema()
    assert schema["properties"]["polarity"]["enum"] == ["assert", "retract"]
    event = _personal_agent_event().model_copy(
        update={"payload": {"content": "I don't drive my BMW anymore."}}
    )
    proposal = {
        "subject": "BMW",
        "statement": "User no longer drives their BMW.",
        "source_event_ids": [7],
        "sensitivity_guess": "internal",
        "claim_kind": "resource",
        "derivation": "direct",
        "evidence_spans": [{"source_event_id": 7, "text": "don't drive my BMW anymore"}],
    }

    retraction = _normalize_distilled_candidate(
        _DistilledCandidate.model_validate({**proposal, "polarity": "retract"}),
        by_sequence={7: event},
        scope="general",
    )
    assert retraction.polarity is Polarity.RETRACT
    assert retraction.statement == "User no longer drives their BMW."
    with pytest.raises(ValueError, match="local validation"):
        _normalize_distilled_candidate(
            _DistilledCandidate.model_validate(proposal),
            by_sequence={7: event},
            scope="general",
        )


@pytest.mark.parametrize(
    ("message", "claim_kind", "statement"),
    [
        (
            "I lead the infrastructure team.",
            MemoryClaimKind.ROLE,
            "User leads an infrastructure team.",
        ),
        (
            "I'm the treasurer for our cycling club.",
            MemoryClaimKind.ROLE,
            "User is treasurer for a cycling club.",
        ),
        (
            "I cannot take meetings on Fridays.",
            MemoryClaimKind.CONSTRAINT,
            "User cannot take meetings on Fridays.",
        ),
        (
            "All code must run on ARM64.",
            MemoryClaimKind.CONSTRAINT,
            "User requires all code to run on ARM64.",
        ),
        (
            "Our runbook is in docs/operations.md.",
            MemoryClaimKind.RESOURCE,
            "The user's runbook is in docs/operations.md.",
        ),
        (
            "I keep tax records in the Blue folder.",
            MemoryClaimKind.RESOURCE,
            "User keeps tax records in the Blue folder.",
        ),
        (
            "Use the staging dashboard for deploy status.",
            MemoryClaimKind.RESOURCE,
            "The staging dashboard is used for deploy status.",
        ),
    ],
)
async def test_high_recall_fallback_covers_durable_must_form_categories(
    message: str,
    claim_kind: MemoryClaimKind,
    statement: str,
) -> None:
    event = _personal_agent_event().model_copy(update={"payload": {"content": message}})

    candidates = await formation.HighRecallCandidateExtractor().extract(
        [event], principal=principal(), scope="general"
    )

    assert any(
        candidate.claim_kind is claim_kind and candidate.statement == statement
        for candidate in candidates
    )


@pytest.mark.parametrize(
    ("message", "claim_kind", "statement"),
    [
        (
            "I want to choose a web-search provider this week.",
            MemoryClaimKind.GOAL,
            "User wants to choose a web-search provider this week.",
        ),
        (
            "My goal is to finish the marathon in under four hours.",
            MemoryClaimKind.GOAL,
            "User wants to finish the marathon in under four hours.",
        ),
        (
            "I work professionally as a software developer.",
            MemoryClaimKind.SKILL,
            "User works professionally as a software developer.",
        ),
        (
            "I'm deeply interested in urban history.",
            MemoryClaimKind.INTEREST,
            "User is interested in urban history.",
        ),
        (
            "I love learning about exoplanets.",
            MemoryClaimKind.INTEREST,
            "User is interested in exoplanets.",
        ),
        (
            "I run every morning before breakfast.",
            MemoryClaimKind.HABIT,
            "User runs every morning before breakfast.",
        ),
        (
            "I review my calendar every Sunday night.",
            MemoryClaimKind.HABIT,
            "User reviews their calendar every Sunday night.",
        ),
        (
            "My knee often hurts after long runs.",
            MemoryClaimKind.RECURRING_STATE,
            "User's knee often hurts after long runs.",
        ),
        (
            "The build regularly slows down after midnight.",
            MemoryClaimKind.RECURRING_STATE,
            "The user's build regularly slows after midnight.",
        ),
        (
            "Dark mode works better for me.",
            MemoryClaimKind.PREFERENCE,
            "User prefers dark mode.",
        ),
        (
            "The project uses PostgreSQL.",
            MemoryClaimKind.PROJECT_FACT,
            "The user's project uses PostgreSQL.",
        ),
        (
            "Production deploys happen through CircleCI.",
            MemoryClaimKind.PROJECT_FACT,
            "The user's production deploys happen through CircleCI.",
        ),
    ],
)
async def test_high_recall_fallback_covers_remaining_direct_must_form_cases(
    message: str,
    claim_kind: MemoryClaimKind,
    statement: str,
) -> None:
    event = _personal_agent_event().model_copy(update={"payload": {"content": message}})

    candidates = await formation.HighRecallCandidateExtractor().extract(
        [event], principal=principal(), scope="general"
    )

    assert any(
        candidate.claim_kind is claim_kind and candidate.statement == statement
        for candidate in candidates
    )


async def test_software_development_cue_forms_only_as_tentative_hypothesis() -> None:
    candidates = await _personal_agent_candidates()
    skill = next(
        candidate
        for candidate in candidates
        if candidate.subject == "software-development experience"
    )

    assert skill.statement == "User likely has software-development experience."
    assert skill.claim_kind == "skill"
    assert skill.derivation == "hypothesis"
    assert skill.longevity == "tentative"
    assert skill.model_confidence == 0.35
    assert not any(
        candidate.statement == "User is a software developer." for candidate in candidates
    )


def test_memory_record_separates_evidence_and_usage_clocks() -> None:
    record = memory()

    assert record.last_evidence_at == record.valid_from
    assert record.last_used_at is None
    assert record.evidence_count == len(set(record.source_event_ids))
    assert record.claim_kind == "preference"
    assert record.derivation == "direct"
    assert record.longevity == "durable"
    assert record.lifecycle_policy_version == "lifecycle@1-backfill"


def test_postgres_mapping_serializes_memory_lifecycle_fields() -> None:
    record = memory().model_copy(
        update={
            "claim_kind": MemoryClaimKind.ONGOING_PROJECT,
            "derivation": MemoryDerivation.HYPOTHESIS,
            "longevity": MemoryLongevity.TENTATIVE,
            "last_used_at": NOW + timedelta(days=1),
            "evidence_count": 3,
            "lifecycle_policy_version": "lifecycle@2",
        }
    )

    values = _memory_values(record)

    assert type(values["claim_kind"]) is str
    assert values["claim_kind"] == "ongoing_project"
    assert type(values["derivation"]) is str
    assert values["derivation"] == "hypothesis"
    assert type(values["longevity"]) is str
    assert values["longevity"] == "tentative"
    assert values["last_evidence_at"] == record.last_evidence_at
    assert values["last_used_at"] == record.last_used_at
    assert values["evidence_count"] == 3
    assert values["lifecycle_policy_version"] == "lifecycle@2"


async def test_usage_has_its_own_clock() -> None:
    clock, factory, service, _retriever = await formation_stack()
    source = await user_event(factory, "User prefers concise answers.")
    formed = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers concise answers.",
        subject="answer style",
        scope="general",
        source_event_ids=[source],
    )
    clock.advance(timedelta(days=1))

    async with factory() as uow:
        moved = await service._move_utility(uow, [formed.id], 0.1, clock.now(), cited=True)
    stored = (await service.list_memories())[0]

    assert moved == 1
    assert stored.utility == 0.1
    assert stored.last_used_at == clock.now()
    assert stored.last_evidence_at == formed.last_evidence_at
    assert stored.last_reinforced_at == formed.last_reinforced_at
    assert stored.confidence == formed.confidence


async def test_every_automatic_claim_is_source_grounded() -> None:
    class UnsupportedSpanExtractor:
        name = "unsupported-span-test"

        async def extract(
            self,
            events: list[EventEnvelope],
            *,
            principal: object,
            scope: str,
        ) -> list[MemoryCandidate]:
            source = next(event for event in events if event.event_type == "user.message.created")
            return [
                _candidate(
                    source_event_ids=[source.sequence],
                    evidence_spans=[
                        {
                            "source_event_id": source.sequence,
                            "text": "running a multinational company",
                        }
                    ],
                )
            ]

    clock, factory, baseline, _retriever = await formation_stack()
    await user_event(factory, "I am building a personal AI agent.")
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=UnsupportedSpanExtractor(),
        policy_version="formation@9",
    )

    result = await service.run(
        trigger="session_closed",
        scope="general",
        session_id=SESSION_ID,
    )

    assert result.beliefs == []
    assert result.run.rejected == 1
    assert not any(
        belief.statement == "User is building a personal AI agent."
        for belief in await service.list_memories()
    )


def test_recall_renders_uncertainty_faithfully() -> None:
    record = memory(statement="User is building a personal AI agent.").model_copy(
        update={
            "subject": "personal AI agent",
            "claim_kind": MemoryClaimKind.ONGOING_PROJECT,
            "derivation": MemoryDerivation.DIRECT,
            "longevity": MemoryLongevity.ONGOING,
        }
    )
    item = _score(
        record,
        recall_query(text="personal AI agent"),
        now=NOW,
    )
    assert item is not None

    rendered = render_memory([item], as_of=NOW)
    view = MemoryView.from_record(record)

    assert item.claim_kind is MemoryClaimKind.ONGOING_PROJECT
    assert item.derivation is MemoryDerivation.DIRECT
    assert item.longevity is MemoryLongevity.ONGOING
    assert "direct, ongoing" in rendered
    assert view.claim_kind is MemoryClaimKind.ONGOING_PROJECT
    assert view.derivation is MemoryDerivation.DIRECT
    assert view.longevity is MemoryLongevity.ONGOING
    assert "model_confidence" not in view.model_dump()


async def _automatic_memory(
    *,
    derivation: MemoryDerivation,
    longevity: MemoryLongevity,
) -> tuple[FixedClock, GovernedMemoryService, MemoryRecord]:
    clock, factory, baseline, _retriever = await formation_stack()
    source = await user_event(factory, "I am building a personal AI agent.")
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=formation.HighRecallCandidateExtractor(),
        policy_version="formation@9",
    )
    record = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement=(
            "User likely has software-development experience."
            if derivation is MemoryDerivation.HYPOTHESIS
            else "User is building a personal AI agent."
        ),
        subject=(
            "software-development experience"
            if derivation is MemoryDerivation.HYPOTHESIS
            else "personal AI agent"
        ),
        scope="general",
        source_event_ids=[source],
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        claim_kind=(
            MemoryClaimKind.SKILL
            if derivation is MemoryDerivation.HYPOTHESIS
            else MemoryClaimKind.ONGOING_PROJECT
        ),
        derivation=derivation,
        longevity=longevity,
    )
    return clock, service, record


async def test_unsupported_hypotheses_retire_after_thirty_days() -> None:
    clock, service, record = await _automatic_memory(
        derivation=MemoryDerivation.HYPOTHESIS,
        longevity=MemoryLongevity.TENTATIVE,
    )
    clock.advance(timedelta(days=29, hours=23))
    assert await service.expire() == []
    clock.advance(timedelta(hours=1))

    expired = await service.expire()

    assert len(expired) == 1
    assert expired[0].id == record.id
    assert expired[0].status is MemoryStatus.RETIRED
    assert expired[0].last_evidence_at == record.last_evidence_at


async def test_unsupported_ongoing_observations_retire_after_ninety_days() -> None:
    clock, service, record = await _automatic_memory(
        derivation=MemoryDerivation.DIRECT,
        longevity=MemoryLongevity.ONGOING,
    )
    clock.advance(timedelta(days=89, hours=23))
    assert await service.expire() == []
    clock.advance(timedelta(hours=1))

    expired = await service.expire()

    assert len(expired) == 1
    assert expired[0].id == record.id
    assert expired[0].status is MemoryStatus.RETIRED
    assert expired[0].last_evidence_at == record.last_evidence_at


async def test_authoritative_edit_cancels_automatic_expiry() -> None:
    clock, service, record = await _automatic_memory(
        derivation=MemoryDerivation.HYPOTHESIS,
        longevity=MemoryLongevity.TENTATIVE,
    )
    clock.advance(timedelta(days=29))

    edited = await service.edit(
        record.id,
        MemoryEdit(statement="User has software-development experience."),
    )
    clock.advance(timedelta(days=2))

    assert await service.expire() == []
    assert edited.authority is MemoryAuthority.USER
    assert edited.derivation is MemoryDerivation.DIRECT
    assert edited.longevity is MemoryLongevity.DURABLE
    assert edited.expires_at is None
    assert edited.last_evidence_at == clock.now() - timedelta(days=2)


async def test_compound_formation_is_bounded_diverse_and_accounted() -> None:
    class BroadExtractor:
        name = "broad-compound-test"

        async def extract(
            self,
            events: list[EventEnvelope],
            *,
            principal: Principal,
            scope: str,
        ) -> list[MemoryCandidate]:
            del principal
            proposed: list[MemoryCandidate] = []
            sources = [event for event in events if event.event_type == "user.message.created"]
            kinds = list(MemoryClaimKind)[:8]
            for event in sources:
                evidence = event.payload["content"]
                assert isinstance(evidence, str)
                for index in range(8):
                    direct = index < 4
                    proposed.append(
                        _candidate(
                            source_event_ids=[event.sequence],
                            evidence_spans=[{"source_event_id": event.sequence, "text": evidence}],
                            subject=f"subject-{event.sequence}-{index}",
                            statement=(
                                f"User has project detail {event.sequence}-{index}."
                                if direct
                                else f"User likely has skill {event.sequence}-{index}."
                            ),
                            claim_kind=kinds[index],
                            derivation=("direct" if direct else "hypothesis"),
                            longevity=("ongoing" if direct else "tentative"),
                            model_confidence=(0.65 if direct else 0.35),
                            proposed_scope=scope,
                        )
                    )
            return proposed

    clock, factory, baseline, _retriever = await formation_stack()
    for index in range(7):
        await user_event(factory, f"Compound source {index} with useful context.")
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=BroadExtractor(),
        policy_version="formation@9",
    )

    result = await service.run(
        trigger="session_closed",
        scope="general",
        session_id=SESSION_ID,
    )

    per_source: dict[int, int] = {}
    for belief in result.beliefs:
        for source_id in belief.source_event_ids:
            per_source[source_id] = per_source.get(source_id, 0) + 1
    assert result.run.candidates_proposed == 56
    assert len(result.beliefs) == 32
    assert result.run.rejected == 24
    assert max(per_source.values()) <= 6
    assert sum(belief.derivation is MemoryDerivation.DIRECT for belief in result.beliefs) == 28
    assert len({belief.subject for belief in result.beliefs}) == 32


async def test_per_source_displacement_is_reported_separately() -> None:
    class SingleSourceExtractor:
        name = "single-source-cap-test"

        async def extract(
            self,
            events: list[EventEnvelope],
            *,
            principal: Principal,
            scope: str,
        ) -> list[MemoryCandidate]:
            del principal
            event = next(item for item in events if item.event_type == "user.message.created")
            evidence = str(event.payload["content"])
            return [
                _candidate(
                    source_event_ids=[event.sequence],
                    evidence_spans=[{"source_event_id": event.sequence, "text": evidence}],
                    subject=f"source subject {index}",
                    statement=f"User has source fact {index}.",
                    proposed_scope=scope,
                )
                for index in range(8)
            ]

    clock, factory, baseline, _retriever = await formation_stack()
    await user_event(factory, "One source supports several future-useful facts.")
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=SingleSourceExtractor(),
        policy_version="formation@9",
    )

    result = await service.run(
        trigger="session_closed",
        scope="general",
        session_id=SESSION_ID,
    )

    assert len(result.beliefs) == 6
    assert result.run.decision_counts["displaced_per_source"] == 2
    assert "displaced_global" not in result.run.decision_counts


async def test_later_evidence_promotes_instead_of_duplicating() -> None:
    clock, factory, baseline, _retriever = await formation_stack()
    first_source = await user_event(factory, "I am building an AI agent.")
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=formation.HighRecallCandidateExtractor(),
        policy_version="formation@9",
    )
    hypothesis = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User likely has software-development experience.",
        subject="software-development experience",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[first_source],
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        claim_kind=MemoryClaimKind.SKILL,
        derivation=MemoryDerivation.HYPOTHESIS,
        longevity=MemoryLongevity.TENTATIVE,
    )
    clock.advance(timedelta(days=1))
    second_source = await user_event(factory, "I have software-development experience.")

    promoted = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User has software-development experience.",
        subject="software-development experience",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[second_source],
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        claim_kind=MemoryClaimKind.SKILL,
        derivation=MemoryDerivation.DIRECT,
        longevity=MemoryLongevity.DURABLE,
    )
    live = await service.list_memories()

    assert promoted.id == hypothesis.id
    assert len(live) == 1
    assert promoted.derivation is MemoryDerivation.DIRECT
    assert promoted.longevity is MemoryLongevity.DURABLE
    assert promoted.statement == "User has software-development experience."
    assert promoted.source_event_ids == [first_source, second_source]
    assert promoted.evidence_count == 2
    assert promoted.last_evidence_at == clock.now()


async def test_only_later_evidence_refreshes_the_evidence_clock() -> None:
    clock, factory, baseline, _retriever = await formation_stack()
    first_source = await user_event(factory, "I am building a personal AI agent.")
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=formation.HighRecallCandidateExtractor(),
        policy_version="formation@9",
    )

    async def observe(source_id: int) -> MemoryRecord:
        return await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement="User is building a personal AI agent.",
            subject="personal AI agent",
            scope="general",
            belief_type=BeliefType.USER_MODEL_ATTR,
            source_event_ids=[source_id],
            explicit=False,
            authority=MemoryAuthority.INFERRED,
            claim_kind=MemoryClaimKind.ONGOING_PROJECT,
            derivation=MemoryDerivation.DIRECT,
            longevity=MemoryLongevity.ONGOING,
        )

    formed = await observe(first_source)
    clock.advance(timedelta(days=5))
    replayed = await observe(first_source)
    assert replayed.last_evidence_at == formed.last_evidence_at
    assert replayed.evidence_count == 1

    second_source = await user_event(factory, "I am building a personal AI agent.")
    reinforced = await observe(second_source)
    assert reinforced.id == formed.id
    assert reinforced.last_evidence_at == clock.now()
    assert reinforced.evidence_count == 2

    clock.advance(timedelta(days=1))
    async with factory() as uow:
        await service._move_utility(uow, [formed.id], 0.1, clock.now(), cited=True)
    cited = await service.get_memory(formed.id)
    assert cited.last_used_at == clock.now()
    assert cited.last_evidence_at == reinforced.last_evidence_at
    assert cited.evidence_count == 2


def _resolved_model() -> ResolvedModel:
    return ResolvedModel(
        provider="fake",
        model="scripted",
        policy_name="memory-distillation",
        resolved_at=NOW,
    )


async def _distillation_extractor(
    responses: list[str],
) -> tuple[
    NemoriAssistedCandidateExtractor,
    FakeModelProvider,
    MemoryUnitOfWorkFactory,
]:
    clock, factory, baseline, _retriever = await formation_stack()
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text=response) for response in responses]),
        clock,
    )
    extractor = NemoriAssistedCandidateExtractor(
        provider=provider,
        resolved_model=_resolved_model(),
        uow_factory=factory,
        clock=clock,
        ids=baseline._ids,
    )
    return extractor, provider, factory


def _empty_distillation(*source_ids: int) -> str:
    return json.dumps(
        {
            "candidates": [],
            "coverage": [
                {
                    "coverage_unit_id": f"{source_id}:1",
                    "decision": "not_memory",
                    "candidate_indexes": [],
                    "prediction_indexes": [],
                }
                for source_id in source_ids
            ],
        }
    )


async def test_one_consolidation_makes_exactly_three_batched_calls() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am building a personal AI agent.")
    responses = [
        json.dumps(
            {
                "episodes": [
                    {
                        "narrative": f"[e:{source_id}] I am building a personal AI agent.",
                        "subjects": ["personal AI agent"],
                        "source_event_ids": [source_id],
                    }
                ]
            }
        ),
        '{"predictions":[]}',
        _empty_distillation(source_id),
    ]
    provider._script.turns = [ScriptedTurn(text=response) for response in responses]

    candidates = await extractor.extract(
        await session_events(factory), principal=principal(), scope="general"
    )

    assert len(provider.requests) == 3
    assert [request.metadata["stage"] for request in provider.requests] == [
        "episode_integration",
        "anticipation",
        "prediction_error_distillation",
    ]
    distillation_instruction = provider.requests[2].conversation[0]
    assert isinstance(distillation_instruction, SystemMessage)
    instruction = distillation_instruction.content[0]
    assert isinstance(instruction, TextPart)
    assert "Favor useful recall over timidity" in instruction.text
    assert "ongoing activities" in instruction.text
    assert "store it as a hypothesis" in instruction.text
    assert "exact evidence_spans" in instruction.text
    assert "ambiguous, inferred, ongoing, or sensitive" in instruction.text
    assert extractor.last_audit.provider_calls == 3
    assert any(
        candidate.statement == "User is building a personal AI agent." for candidate in candidates
    )
    async with factory() as uow:
        episodes = await uow.episodes.for_session(SESSION_ID, principal())
    assert len(episodes) == 1
    assert episodes[0].source_event_ids == [source_id]


async def test_consolidation_persists_content_free_metrics_for_each_provider_stage() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am building a personal AI agent.")
    responses = [
        json.dumps(
            {
                "episodes": [
                    {
                        "narrative": f"[e:{source_id}] I am building a personal AI agent.",
                        "subjects": ["personal AI agent"],
                        "source_event_ids": [source_id],
                    }
                ]
            }
        ),
        '{"predictions":[]}',
        _empty_distillation(source_id),
    ]
    provider._script.turns = [
        ScriptedTurn(
            text=response,
            delay_ms=index,
            usage=ModelUsage(
                input_tokens=10 * index,
                output_tokens=index,
                cost=Decimal(f"0.0{index}"),
            ),
        )
        for index, response in enumerate(responses, start=1)
    ]
    clock, _ignored_factory, baseline, _retriever = await formation_stack()
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=extractor,
        policy_version="formation@9",
    )

    result = await service.run(
        trigger="session_closed",
        scope="general",
        session_id=SESSION_ID,
    )

    assert result.run.provider_stage_metrics == {
        "episode_integration": {
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cost_usd": "0.01",
            "latency_ms": 1,
            "outcome": "success",
        },
        "anticipation": {
            "input_tokens": 20,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cost_usd": "0.02",
            "latency_ms": 2,
            "outcome": "success",
        },
        "prediction_error_distillation": {
            "input_tokens": 30,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 3,
            "reasoning_tokens": 0,
            "cost_usd": "0.03",
            "latency_ms": 3,
            "outcome": "success",
        },
    }


async def test_stage_metrics_preserve_partial_usage_from_provider_failure() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am building a personal AI agent.")
    provider._script.turns = [
        ScriptedTurn(
            fail_with=ModelTransientError(
                provider="fake",
                model="scripted",
                attempt_id=UUID(int=999),
                message="temporary",
            ),
            usage=ModelUsage(
                input_tokens=17,
                output_tokens=4,
                cost=Decimal("0.07"),
            ),
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(text=_empty_distillation(source_id)),
    ]

    await extractor.extract(await session_events(factory), principal=principal(), scope="general")

    assert extractor.last_audit.provider_stage_metrics["episode_integration"] == {
        "input_tokens": 17,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 4,
        "reasoning_tokens": 0,
        "cost_usd": "0.07",
        "latency_ms": 0,
        "outcome": "provider_failure",
    }


async def test_episode_integration_partitions_topics_and_anticipates_from_prefixes() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    first = await user_event(factory, "PRIOR_ACTIVITY I swim three mornings each week.")
    second = await user_event(factory, "CURRENT_HOME I moved to Portland last month.")
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "episodes": [
                        {
                            "narrative": (
                                f"[e:{first}] PRIOR_ACTIVITY I swim three mornings each week."
                            ),
                            "subjects": ["swimming"],
                            "source_event_ids": [first],
                        },
                        {
                            "narrative": (
                                f"[e:{second}] CURRENT_HOME I moved to Portland last month."
                            ),
                            "subjects": ["home location"],
                            "source_event_ids": [second],
                        },
                    ]
                }
            )
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(text=_empty_distillation(first, second)),
    ]

    await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="general",
    )

    async with factory() as uow:
        episodes = await uow.episodes.for_session(SESSION_ID, principal())
    assert [episode.source_event_ids for episode in episodes] == [[first], [second]]
    assert extractor.last_audit.episode_count == 2

    anticipation = provider.requests[1].conversation[1]
    assert isinstance(anticipation, UserMessage)
    prompt = anticipation.content[0]
    assert isinstance(prompt, TextPart)
    payload = json.loads(prompt.text)
    assert set(payload) == {"prefix_events", "episode_cues", "prior_memories"}
    assert payload["episode_cues"] == [
        {"before_event_sequence": first, "episode_index": 0},
        {"before_event_sequence": second, "episode_index": 1},
    ]
    # One request anticipates both episodes, so the prefix stops before the
    # earliest episode: neither episode's own evidence is present.
    assert payload["prefix_events"] == []
    assert "PRIOR_ACTIVITY" not in prompt.text
    assert "CURRENT_HOME" not in prompt.text


async def test_distillation_normalizes_provider_policy_fields_locally() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am prototyping an app for my class.")
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "episodes": [
                        {
                            "narrative": f"[e:{source_id}] I am prototyping an app for my class.",
                            "subjects": ["software-development experience"],
                            "source_event_ids": [source_id],
                        }
                    ]
                }
            )
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "subject": "software-development experience",
                            "statement": "User may have software-development experience.",
                            "source_event_ids": [source_id],
                            "sensitivity_guess": "public",
                            "claim_kind": "skill",
                            "derivation": "hypothesis",
                            "evidence_spans": [
                                {
                                    "source_event_id": source_id,
                                    "text": "prototyping an app",
                                }
                            ],
                        }
                    ],
                    "coverage": [
                        {
                            "coverage_unit_id": f"{source_id}:1",
                            "decision": "formed",
                            "candidate_indexes": [0],
                            "prediction_indexes": [],
                        }
                    ],
                }
            )
        ),
    ]

    candidates = await extractor.extract(
        await session_events(factory), principal=principal(), scope="general"
    )
    skill = next(candidate for candidate in candidates if candidate.claim_kind == "skill")

    assert skill.belief_type == "user_model_attr"
    assert skill.model_confidence == 0.35
    assert skill.proposed_portability == "portable"
    assert skill.sensitivity_guess == "internal"
    assert skill.longevity == "tentative"
    assert skill.statement == "User may have software-development experience."
    assert skill.valid_from == NOW
    assert skill.expires_hint is None


async def test_distillation_rejects_an_incomplete_source_coverage_ledger() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(
        factory,
        "I am building a personal AI agent and I prefer concise answers.",
    )
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "episodes": [
                        {
                            "narrative": (
                                f"[e:{source_id}] I am building a personal AI agent and I prefer "
                                "concise answers."
                            ),
                            "subjects": ["personal AI agent", "answer style"],
                            "source_event_ids": [source_id],
                        }
                    ]
                }
            )
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(text=_empty_distillation(source_id)),
    ]

    await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="general",
    )

    assert "prediction_error_distillation" in extractor.last_audit.fallback_stages
    assert extractor.last_audit.failure_kinds["prediction_error_distillation"] == "validation"


async def test_one_invalid_provider_candidate_does_not_discard_valid_siblings() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(
        factory,
        "I prefer concise answers. Use the staging dashboard for deploy status.",
    )
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "episodes": [
                        {
                            "narrative": (
                                f"[e:{source_id}] I prefer concise answers.\n"
                                f"[e:{source_id}] Use the staging dashboard for deploy status."
                            ),
                            "subjects": ["answer style", "deploy status"],
                            "source_event_ids": [source_id],
                        }
                    ]
                }
            )
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "subject": "answer style",
                            "statement": "User prefers concise answers.",
                            "source_event_ids": [source_id],
                            "sensitivity_guess": "internal",
                            "claim_kind": "preference",
                            "derivation": "direct",
                            "evidence_spans": [
                                {
                                    "source_event_id": source_id,
                                    "text": "I prefer concise answers",
                                }
                            ],
                        },
                        {
                            "subject": "User",
                            "statement": (
                                "User uses the staging dashboard to check deploy status."
                            ),
                            "source_event_ids": [source_id],
                            "sensitivity_guess": "internal",
                            "claim_kind": "habit",
                            "derivation": "direct",
                            "evidence_spans": [
                                {
                                    "source_event_id": source_id,
                                    "text": "Use the staging dashboard for deploy status",
                                }
                            ],
                        },
                    ],
                    "coverage": [
                        {
                            "coverage_unit_id": f"{source_id}:1",
                            "decision": "formed",
                            "candidate_indexes": [0],
                            "prediction_indexes": [],
                        },
                        {
                            "coverage_unit_id": f"{source_id}:2",
                            "decision": "formed",
                            "candidate_indexes": [1],
                            "prediction_indexes": [],
                        },
                    ],
                }
            )
        ),
    ]

    candidates = await extractor.extract(
        await session_events(factory),
        principal=principal(),
        scope="general",
    )

    assert any(candidate.statement == "User prefers concise answers." for candidate in candidates)
    assert extractor.last_audit.provider_candidates == 1
    assert extractor.last_audit.rejected_provider_candidates == 1
    assert (
        extractor.last_audit.provider_stage_metrics["prediction_error_distillation"]["outcome"]
        == "partial_validation"
    )
    assert "episode_integration" not in extractor.last_audit.fallback_stages


async def test_coverage_cannot_call_evidence_represented_without_attributed_memory() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I swim three mornings each week.")
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "episodes": [
                        {
                            "narrative": f"[e:{source_id}] I swim three mornings each week.",
                            "subjects": ["swimming"],
                            "source_event_ids": [source_id],
                        }
                    ]
                }
            )
        ),
        ScriptedTurn(
            text=json.dumps(
                {
                    "predictions": [
                        {
                            "episode_index": 0,
                            "statement": "User swims regularly.",
                            "attributed_memory_ids": [],
                        }
                    ]
                }
            )
        ),
        ScriptedTurn(
            text=json.dumps(
                {
                    "candidates": [],
                    "coverage": [
                        {
                            "coverage_unit_id": f"{source_id}:1",
                            "decision": "represented",
                            "candidate_indexes": [],
                            "prediction_indexes": [0],
                        }
                    ],
                }
            )
        ),
    ]

    await extractor.extract(await session_events(factory), principal=principal(), scope="general")

    assert "prediction_error_distillation" in extractor.last_audit.fallback_stages


def test_provider_cannot_render_a_hypothesis_as_an_unqualified_fact() -> None:
    """A hypothesis the provider wrote as a fact is rejected, never rewritten.

    Local code owns confidence and longevity; it does not own the hedge. Adding
    one would let the provider store an inference as a fact and have the
    platform soften it after the fact, and strengthening "may" into "likely"
    would move a belief's uncertainty without any evidence moving it.
    """

    candidate = _candidate(
        claim_kind="skill",
        derivation="hypothesis",
        longevity="tentative",
        subject="software-development experience",
        statement="User is a software developer.",
        evidence_spans=[
            {
                "source_event_id": 7,
                "text": "building a personal AI agent",
            }
        ],
    )

    with pytest.raises(ValueError, match="uncertainty language"):
        _normalize_provider_candidate(
            candidate,
            by_sequence={7: _personal_agent_event()},
            scope="general",
        )

    hedged = _normalize_provider_candidate(
        candidate.model_copy(update={"statement": "User may be a software developer."}),
        by_sequence={7: _personal_agent_event()},
        scope="general",
    )
    assert hedged.statement == "User may be a software developer."
    assert hedged.model_confidence == 0.35
    assert hedged.longevity == "tentative"


def test_local_policy_canonicalizes_explicit_avoidance_as_a_constraint() -> None:
    event = _personal_agent_event().model_copy(
        update={"payload": {"content": "I avoid flights with overnight layovers."}}
    )
    candidate = _candidate(
        claim_kind="preference",
        subject="User",
        statement="User avoids flights with overnight layovers.",
        evidence_spans=[{"source_event_id": 7, "text": "avoid flights with overnight layovers"}],
    )

    normalized = _normalize_provider_candidate(
        candidate,
        by_sequence={7: event},
        scope="general",
    )

    assert normalized.claim_kind is MemoryClaimKind.CONSTRAINT
    assert normalized.belief_type is BeliefType.USER_MODEL_ATTR
    assert normalized.longevity is MemoryLongevity.DURABLE


def test_local_policy_rejects_imperative_rewritten_as_a_user_habit() -> None:
    event = _personal_agent_event().model_copy(
        update={"payload": {"content": "Use the staging dashboard for deploy status."}}
    )
    candidate = _candidate(
        claim_kind="habit",
        subject="User",
        statement="User uses the staging dashboard to check deploy status.",
        evidence_spans=[
            {"source_event_id": 7, "text": "Use the staging dashboard for deploy status"}
        ],
    )

    with pytest.raises(ValueError, match="imperative evidence"):
        _normalize_provider_candidate(
            candidate,
            by_sequence={7: event},
            scope="general",
        )


def test_semantic_duplicate_detection_merges_provider_paraphrases_only() -> None:
    deterministic = _candidate(
        subject="calisthenics experience",
        statement="User trained with calisthenics for many years before restarting 5x5.",
        claim_kind="skill",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "many years of calisthenics"}],
    )
    paraphrase = _candidate(
        subject="User",
        statement="User practiced calisthenics for many years before restarting 5x5.",
        claim_kind="skill",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "many years of calisthenics"}],
    )
    distinct = _candidate(
        subject="5x5 training history",
        statement="User restarted 5x5 about one year ago.",
        claim_kind="skill",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "restarted 5x5 a year ago"}],
    )
    different_activity = _candidate(
        subject="running",
        statement="User regularly runs on non-strength-training days.",
        claim_kind="habit",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "I swim, run, or bike"}],
    )
    swimming = different_activity.model_copy(
        update={
            "subject": "swimming",
            "statement": "User regularly swims on non-strength-training days.",
        }
    )
    swimming_paraphrase = swimming.model_copy(
        update={
            "statement": (
                "User includes swimming among their activities on days without 5x5 "
                "strength training."
            )
        }
    )
    hyphenated_cross_kind_paraphrase = _candidate(
        subject="User",
        statement=(
            "User practiced a gymnastic strength-training routine for approximately "
            "six years, though the exact duration is uncertain."
        ),
        claim_kind="project_fact",
        source_event_ids=[9],
        evidence_spans=[
            {
                "source_event_id": 9,
                "text": "gymnastic strength training routine for about six years",
            }
        ],
    )
    gymnastic_training = hyphenated_cross_kind_paraphrase.model_copy(
        update={
            "subject": "gymnastic strength training",
            "statement": (
                "User trained with a gymnastic strength training routine for "
                "approximately six years, but is uncertain of the exact duration."
            ),
            "claim_kind": MemoryClaimKind.SKILL,
        }
    )
    specific_interest = _candidate(
        subject="exoplanets",
        statement="User is interested in exoplanets.",
        claim_kind="interest",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "love learning about exoplanets"}],
    )
    generic_interest = specific_interest.model_copy(
        update={
            "subject": "User",
            "statement": "User loves learning about exoplanets.",
        }
    )

    assert _candidates_semantically_duplicate(deterministic, paraphrase)
    assert _candidates_semantically_duplicate(
        deterministic,
        paraphrase.model_copy(update={"claim_kind": MemoryClaimKind.PROJECT_FACT}),
    )
    assert not _candidates_semantically_duplicate(deterministic, distinct)
    assert not _candidates_semantically_duplicate(swimming, different_activity)
    assert _candidates_semantically_duplicate(swimming, swimming_paraphrase)
    assert _candidates_semantically_duplicate(
        gymnastic_training,
        hyphenated_cross_kind_paraphrase,
    )
    assert _candidates_semantically_duplicate(specific_interest, generic_interest)
    # Production formed both of these from one sentence of the 5x5 conversation:
    # a digit range and a spelled-out range are the same count, and a light
    # verb is not content, so they are one memory. A goal about the routine
    # carries no count and stays separate.
    routine_project = _candidate(
        subject="5x5 strength training routine",
        statement=(
            "User currently follows the standard 5x5 strength training routine "
            "2\u20133 times per week."
        ),
        claim_kind="ongoing_project",
        source_event_ids=[2],
        evidence_spans=[
            {
                "source_event_id": 2,
                "text": "standard 5x5 strength training routine 2-3 times per week",
            }
        ],
    )
    routine_habit = routine_project.model_copy(
        update={
            "subject": "standard 5x5 strength training routine",
            "statement": (
                "User does the standard 5x5 strength training routine two to three times per week."
            ),
            "claim_kind": MemoryClaimKind.HABIT,
        }
    )
    routine_goal = routine_project.model_copy(
        update={
            "subject": "standard 5x5 strength training routine improvement",
            "statement": "User wants to improve their standard 5x5 strength training routine.",
            "claim_kind": MemoryClaimKind.GOAL,
        }
    )
    assert _candidates_semantically_duplicate(routine_project, routine_habit)
    assert not _candidates_semantically_duplicate(routine_habit, routine_goal)
    # An elaboration is more memory, not the same memory: the shorter claim
    # must not absorb the one that names Robert and Berlin.
    son = _candidate(
        subject="son",
        statement="User has a son.",
        claim_kind="relationship",
        source_event_ids=[3],
        evidence_spans=[{"source_event_id": 3, "text": "my son Robert"}],
    )
    son_in_berlin = son.model_copy(
        update={
            "subject": "son Robert",
            "statement": "User has a son named Robert who lives in Berlin.",
            "claim_kind": MemoryClaimKind.PROJECT_FACT,
        }
    )
    assert not _candidates_semantically_duplicate(son, son_in_berlin)


@pytest.mark.parametrize(
    ("claim_kind", "expected"),
    [
        (MemoryClaimKind.ROLE, MemoryLongevity.DURABLE),
        (MemoryClaimKind.CONSTRAINT, MemoryLongevity.DURABLE),
        (MemoryClaimKind.RECURRING_STATE, MemoryLongevity.ONGOING),
        (MemoryClaimKind.RESOURCE, MemoryLongevity.DURABLE),
    ],
)
def test_direct_claim_longevity_is_owned_by_local_policy(
    claim_kind: MemoryClaimKind,
    expected: MemoryLongevity,
) -> None:
    candidate = _candidate(claim_kind=claim_kind, longevity="tentative")

    normalized = _normalize_provider_candidate(
        candidate,
        by_sequence={7: _personal_agent_event()},
        scope="general",
    )

    assert normalized.longevity is expected


async def test_anticipation_is_causally_blinded() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(
        factory,
        "CURRENT_EVIDENCE_SENTINEL I am building a personal AI agent.",
    )
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "episodes": [
                        {
                            "narrative": (
                                f"[e:{source_id}] CURRENT_EVIDENCE_SENTINEL I am building a "
                                "personal AI agent."
                            ),
                            "subjects": [],
                            "source_event_ids": [source_id],
                        }
                    ]
                }
            )
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(text=_empty_distillation(source_id)),
    ]

    await extractor.extract(await session_events(factory), principal=principal(), scope="general")

    anticipation = provider.requests[1]
    user_prompt = anticipation.conversation[1]
    assert isinstance(user_prompt, UserMessage)
    prompt = user_prompt.content[0]
    assert isinstance(prompt, TextPart)
    payload = prompt.text
    assert "CURRENT_EVIDENCE_SENTINEL" not in payload
    assert "narrative" not in payload
    assert "source_events" not in payload
    assert "gold" not in payload
    assert set(json.loads(payload)) == {"prefix_events", "episode_cues", "prior_memories"}
    assert json.loads(payload)["prefix_events"] == []


async def test_every_provider_stage_has_an_audited_deterministic_fallback() -> None:
    extractor, provider, factory = await _distillation_extractor(
        ["not-json", "also-not-json", "still-not-json"]
    )
    await user_event(factory, "I am building a personal AI agent.")

    candidates = await extractor.extract(
        await session_events(factory), principal=principal(), scope="general"
    )

    assert len(provider.requests) == 3
    assert extractor.last_audit.fallback_stages == [
        "episode_integration",
        "anticipation",
        "prediction_error_distillation",
    ]
    assert set(extractor.last_audit.failure_kinds.values()) == {"validation"}
    assert "personal AI agent" not in extractor.last_audit.model_dump_json()
    assert any(
        candidate.statement == "User is building a personal AI agent." for candidate in candidates
    )


async def test_invalid_provider_episode_subjects_use_the_validated_fallback() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am building a personal AI agent.")
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "episodes": [
                        {
                            "narrative": f"[e:{source_id}] I am building a personal AI agent.",
                            "subjects": ["personal AI agent", "PERSONAL AI AGENT"],
                            "source_event_ids": [source_id],
                        }
                    ]
                }
            )
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(text=_empty_distillation(source_id)),
    ]

    await extractor.extract(await session_events(factory), principal=principal(), scope="general")

    assert "episode_integration" in extractor.last_audit.fallback_stages
    async with factory() as uow:
        [episode] = await uow.episodes.for_session(SESSION_ID, principal())
    assert episode.subjects == ["personal AI agent", "software-development experience"]


async def test_provider_fallback_completes_and_schedules_a_provider_repass() -> None:
    """A retryable failure keeps the fallback's memories and re-reads the evidence later.

    formation@9 completes every consolidation with its audited fallback, so the
    watermark advances; before this change that made a provider outage
    permanently consume the richest evidence, the failure that lost the
    production strength-training session. Now the same run also schedules a
    bounded re-pass over the consumed range, and the re-pass adds what the
    provider finds without duplicating what the fallback already formed.
    """

    extractor, provider, factory = await _distillation_extractor(
        ["not-json", "also-not-json", "still-not-json"]
    )
    source_id = await user_event(factory, "I am building a personal AI agent.")
    clock = extractor._clock
    assert isinstance(clock, FixedClock)
    ids = extractor._ids
    service = GovernedMemoryService(
        factory,
        clock,
        ids,
        principal(),
        extractor=extractor,
        policy_version="formation@9",
    )

    result = await service.run(trigger="session_closed", scope="general", session_id=SESSION_ID)

    assert len(provider.requests) == 3
    assert result.run.watermark_after == source_id
    assert result.run.committed == 2
    assert result.run.fallback_stages == [
        "episode_integration",
        "anticipation",
        "prediction_error_distillation",
    ]
    [request] = [
        event
        for event in await session_events(factory)
        if event.event_type == "memory.formation.requested"
        and event.payload.get("trigger") == "provider_retry"
    ]
    assert request.sequence > source_id
    assert request.payload["attempt_number"] == 2
    assert request.payload["source_watermark_before"] == 0
    assert request.payload["source_watermark_after"] == source_id
    diagnosis = await service.diagnose(SESSION_ID)
    assert diagnosis.pending_retry

    provider._script.turns = [
        *provider._script.turns,
        ScriptedTurn(text=_scripted_episode([source_id], ["I am building a personal AI agent."])),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "subject": "AI agents",
                            "statement": "User is interested in AI agents.",
                            "source_event_ids": [source_id],
                            "sensitivity_guess": "internal",
                            "claim_kind": "interest",
                            "derivation": "direct",
                            "evidence_spans": [
                                {"source_event_id": source_id, "text": "personal AI agent"}
                            ],
                        }
                    ],
                    "coverage": [
                        {
                            "coverage_unit_id": f"{source_id}:1",
                            "decision": "formed",
                            "candidate_indexes": [0],
                            "prediction_indexes": [],
                        }
                    ],
                }
            )
        ),
    ]
    clock.advance(timedelta(minutes=2))

    repass = await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)

    assert len(provider.requests) == 6
    assert repass.run.trigger == "provider_retry"
    assert repass.run.fallback_stages == []
    assert repass.run.watermark_after == request.sequence
    live = {belief.statement for belief in await service.list_memories()}
    assert "User is interested in AI agents." in live
    assert (
        sum(
            belief.statement == "User is building a personal AI agent."
            for belief in await service.list_memories()
        )
        == 1
    )
    assert not (await service.diagnose(SESSION_ID)).pending_retry


async def test_provider_repass_exhausts_after_bounded_attempts() -> None:
    extractor, provider, factory = await _distillation_extractor(["not-json"] * 9)
    clock = extractor._clock
    assert isinstance(clock, FixedClock)
    source_id = await user_event(factory, "I am building a personal AI agent.")
    service = GovernedMemoryService(
        factory,
        clock,
        extractor._ids,
        principal(),
        extractor=extractor,
        policy_version="formation@9",
    )

    attempts = []
    for _attempt in range(formation.PROVIDER_MAX_ATTEMPTS):
        attempts.append(
            await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)
        )
        clock.advance(timedelta(minutes=10))

    assert [run.run.trigger for run in attempts] == [
        "session_idle",
        "provider_retry",
        "provider_retry",
    ]
    assert len(provider.requests) == 3 * formation.PROVIDER_MAX_ATTEMPTS
    assert attempts[0].run.committed == 2
    assert all(run.run.committed == 0 for run in attempts[1:])
    async with factory() as uow:
        exhausted = await uow.process_events.list("memory.provider_extraction.retry_exhausted")
    assert len(exhausted) == 1
    assert exhausted[0].payload["attempt_number"] == formation.PROVIDER_MAX_ATTEMPTS
    assert not (await service.diagnose(SESSION_ID)).pending_retry
    assert (
        sum(
            belief.statement == "User is building a personal AI agent."
            for belief in await service.list_memories()
        )
        == 1
    )
    del source_id


async def test_integrated_episodes_are_coherent_and_grounded() -> None:
    clock, factory, baseline, _retriever = await formation_stack()
    first = await user_event(factory, "I am building a personal AI agent.")
    second = await user_event(factory, "I am comparing web search and fetch tools.")
    events = await session_events(factory)

    episode = deterministic_integrated_episode(
        events,
        principal=principal(),
        episode_id=baseline._ids.new_id(),
        created_at=clock.now(),
        subjects=["personal AI agent", "web tools"],
    )
    validate_integrated_episode(episode, events, principal=principal())

    assert episode.source_event_ids == [first, second]
    assert episode.narrative.splitlines() == [
        f"[e:{first}] I am building a personal AI agent.",
        f"[e:{second}] I am comparing web search and fetch tools.",
    ]
    with pytest.raises(ValueError, match="unsupported"):
        validate_integrated_episode(
            episode.model_copy(
                update={
                    "narrative": (
                        f"[e:{first}] I run a multinational company.\n"
                        f"[e:{second}] I am comparing web search and fetch tools."
                    )
                }
            ),
            events,
            principal=principal(),
        )
    with pytest.raises(ValueError, match="owned"):
        validate_integrated_episode(
            episode.model_copy(update={"principal_id": "foreign-principal"}),
            events,
            principal=principal(),
        )


def test_fallback_episode_normalizes_and_bounds_subjects() -> None:
    episode = deterministic_integrated_episode(
        [_personal_agent_event()],
        principal=principal(),
        episode_id=UUID(int=902),
        created_at=NOW,
        subjects=[
            " MacBook ",
            "macbook",
            "  web \t tools  ",
            "x" * 513,
            *(f"subject-{index}" for index in range(70)),
        ],
    )

    assert episode.subjects[:2] == ["MacBook", "web tools"]
    assert episode.subjects[-1] == "subject-61"
    assert len(episode.subjects) == 64
    assert len({subject.casefold() for subject in episode.subjects}) == 64


def test_integrated_episode_batches_require_one_session() -> None:
    first = _personal_agent_event()
    second = first.model_copy(
        update={
            "id": 8,
            "sequence": 8,
            "session_id": UUID(int=SESSION_ID.int + 1),
            "payload": {"content": "I am comparing web tools."},
        }
    )

    with pytest.raises(ValueError, match="same session"):
        deterministic_integrated_episode(
            [first, second],
            principal=principal(),
            episode_id=UUID(int=903),
            created_at=NOW,
        )

    episode = deterministic_integrated_episode(
        [first, second.model_copy(update={"session_id": SESSION_ID})],
        principal=principal(),
        episode_id=UUID(int=904),
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="same session"):
        validate_integrated_episode(episode, [first, second], principal=principal())


async def test_decision_telemetry_accounts_for_every_proposal() -> None:
    clock, factory, baseline, _retriever = await formation_stack()
    await user_event(factory, "I am building a personal AI agent.")
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=formation.HighRecallCandidateExtractor(),
        policy_version="formation@9",
    )

    result = await service.run(
        trigger="session_closed",
        scope="general",
        session_id=SESSION_ID,
    )

    assert result.run.candidates_proposed == 2
    assert result.run.decision_counts == {
        "committed_direct": 1,
        "committed_hypothesis": 1,
    }
    assert sum(result.run.decision_counts.values()) == result.run.candidates_proposed
    telemetry = result.run.model_dump_json()
    assert "personal AI agent" not in telemetry
    assert "software-development experience" not in telemetry


async def test_predictability_suppresses_only_attributable_redundancy() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am building a personal AI agent.")
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "episodes": [
                        {
                            "narrative": f"[e:{source_id}] I am building a personal AI agent.",
                            "subjects": [],
                            "source_event_ids": [source_id],
                        }
                    ]
                }
            )
        ),
        ScriptedTurn(
            text=(
                '{"predictions":[{"episode_index":0,"statement":"User is building a personal '
                'AI agent.","attributed_memory_ids":[]}]}'
            )
        ),
        ScriptedTurn(text=_empty_distillation(source_id)),
    ]
    candidates = await extractor.extract(
        await session_events(factory), principal=principal(), scope="general"
    )
    assert any(
        candidate.statement == "User is building a personal AI agent." for candidate in candidates
    )

    clock, factory, baseline, _retriever = await formation_stack()
    prior_source = await user_event(factory, "I am building a personal AI agent.")
    prior = await baseline.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User is building a personal AI agent.",
        subject="personal AI agent",
        scope="general",
        belief_type=BeliefType.USER_MODEL_ATTR,
        source_event_ids=[prior_source],
    )
    new_source = await user_event(factory, "I am building a personal AI agent.")
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "episodes": [
                                {
                                    "narrative": (
                                        f"[e:{new_source}] I am building a personal AI agent."
                                    ),
                                    "subjects": [],
                                    "source_event_ids": [new_source],
                                }
                            ]
                        }
                    )
                ),
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "predictions": [
                                {
                                    "episode_index": 0,
                                    "statement": "User is building a personal AI agent.",
                                    "attributed_memory_ids": [str(prior.id)],
                                }
                            ]
                        }
                    )
                ),
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "candidates": [],
                            "coverage": [
                                {
                                    "coverage_unit_id": f"{new_source}:1",
                                    "decision": "represented",
                                    "candidate_indexes": [],
                                    "prediction_indexes": [0],
                                }
                            ],
                        }
                    )
                ),
            ]
        ),
        clock,
    )
    attributed = NemoriAssistedCandidateExtractor(
        provider=provider,
        resolved_model=_resolved_model(),
        uow_factory=factory,
        clock=clock,
        ids=baseline._ids,
    )
    new_events = [event for event in await session_events(factory) if event.sequence == new_source]

    candidates = await attributed.extract(new_events, principal=principal(), scope="general")

    assert not any(
        candidate.statement == "User is building a personal AI agent." for candidate in candidates
    )
    assert attributed.last_audit.prediction_attributed_redundancies == 1
    assert attributed.last_audit.coverage_counts == {"represented": 1}


async def test_corrections_remain_durable_through_distillation() -> None:
    clock, factory, baseline, _retriever = await formation_stack()
    await user_event(factory, "I am building a personal AI agent.")
    service = GovernedMemoryService(
        factory,
        clock,
        baseline._ids,
        principal(),
        extractor=formation.HighRecallCandidateExtractor(),
        policy_version="formation@9",
    )
    first = await service.run(
        trigger="session_closed",
        scope="general",
        session_id=SESSION_ID,
    )
    project = next(
        belief
        for belief in first.beliefs
        if belief.statement == "User is building a personal AI agent."
    )
    await service.reject(project.id, RejectionKind.UNTRUE)

    replay = await service.run(
        trigger="rederive",
        scope="general",
        session_id=SESSION_ID,
        since_watermark=0,
    )

    assert replay.run.committed == 0
    assert replay.run.decision_counts["rejected_correction"] >= 1
    assert not any(
        belief.statement == "User is building a personal AI agent."
        for belief in await service.list_memories()
    )


def _passing_distillation_evidence() -> MemoryDistillationEvidence:
    return MemoryDistillationEvidence(
        model_policy="memory-distillation",
        provider="fake",
        model="scripted",
        policy_profile="default",
        policy_version="policy@1",
        scorer_version="distillation-scorer@3",
        build_ref="0123456789abcdef0123456789abcdef01234567",
        corpus_sha256="a" * 64,
        sample_count=60,
        positive_case_count=48,
        seeded_case_count=12,
        represented_case_count=3,
        direct_must_form_recall=0.96,
        hypothesis_must_form_recall=0.82,
        benign_precision=0.92,
        useful_recall_lift_percentage_points=18,
        correction_rate_per_hundred=4,
        evidence_disposition_precision=0.9,
        provider_calls_per_segment=3,
        provider_calls_measured=183,
        consolidations_measured=61,
        provider_cost_usd="1.25",
        boundary_failures=0,
        evaluated_at=NOW,
    )


def test_distillation_activation_is_exact_and_evidence_bound() -> None:
    evidence = _passing_distillation_evidence()
    model = _resolved_model()

    assert (
        select_distillation_policy(
            evidence,
            model,
            "default",
            "policy@1",
            mode="auto",
        )
        == "formation@9"
    )
    assert (
        select_distillation_policy(
            evidence,
            model.model_copy(update={"model": "different"}),
            "default",
            "policy@1",
            mode="auto",
        )
        == "formation@8"
    )
    assert (
        select_distillation_policy(
            evidence,
            model,
            "different-profile",
            "policy@1",
            mode="auto",
        )
        == "formation@8"
    )
    assert (
        select_distillation_policy(
            evidence,
            model,
            "default",
            "different-policy",
            mode="auto",
        )
        == "formation@8"
    )
    assert (
        select_distillation_policy(
            evidence,
            model,
            "default",
            "policy@1",
            mode="off",
        )
        == "formation@8"
    )
    with pytest.raises(ValueError, match="requires matching"):
        select_distillation_policy(
            evidence.model_copy(update={"build_ref": "other-build", "model": "wrong"}),
            model,
            "default",
            "policy@1",
            mode="required",
        )


def test_comparative_evidence_proves_marked_useful_recall_lift() -> None:
    evidence = _passing_distillation_evidence()
    assert evidence.comparative_policies == (
        "formation@7",
        "formation@8",
        "formation@9",
    )

    failing_values = {
        "sample_count": 59,
        "positive_case_count": 41,
        "direct_must_form_recall": 0.94,
        "hypothesis_must_form_recall": 0.79,
        "benign_precision": 0.89,
        "useful_recall_lift_percentage_points": 14.9,
        "correction_rate_per_hundred": 10.1,
        "provider_calls_per_segment": 2,
        "provider_calls_measured": 182,
        "consolidations_measured": 0,
        "boundary_failures": 1,
        "evidence_disposition_precision": 0.74,
        "seeded_case_count": 0,
        "represented_case_count": 0,
        "provider_cost_usd": "999999999",
        "build_ref": "content-6973e8ddc75c6e40947e1f368abf2a96",
        "scorer_version": "distillation-scorer@1",
    }
    baseline = evidence.model_dump(mode="python")
    for field, value in failing_values.items():
        with pytest.raises(ValidationError):
            MemoryDistillationEvidence.model_validate({**baseline, field: value})


def test_formation_corpus_v3_has_declared_coverage() -> None:
    root = Path(__file__).resolve().parents[2]
    corpus, digest = load_distillation_corpus(root)

    positives = [case for case in corpus.cases if case.label != "must_not_form"]
    assert len(corpus.cases) >= 60
    assert len(positives) / len(corpus.cases) >= 0.7
    assert len({case.id for case in corpus.cases}) == len(corpus.cases)
    assert len(digest) == 64
    assert {expected.claim_kind for case in positives for expected in case.expected} == set(
        MemoryClaimKind
    )
    assert {case.label for case in corpus.cases} == {
        "must_form",
        "reasonable_to_form",
        "must_not_form",
    }
    assert {
        "personal-agent",
        "compound",
        "misleading-professional-cue",
        "evidence-promotion",
        "lifecycle-retirement",
        "self-citation",
        "rich-conversation",
    } <= {case.scenario for case in corpus.cases}


def _scripted_episode(source_ids: list[int], texts: list[str]) -> str:
    return json.dumps(
        {
            "episodes": [
                {
                    "narrative": "\n".join(
                        f"[e:{source_id}] {text}"
                        for source_id, text in zip(source_ids, texts, strict=True)
                    ),
                    "subjects": [],
                    "source_event_ids": source_ids,
                }
            ]
        }
    )


async def test_high_recall_fallback_never_fabricates_from_control_turns() -> None:
    """A safety net must abstain rather than invent training memories.

    These turns share surface phrasing with the production training
    conversation (an improvement question, an age question, a duration, a
    'cannot', a 'want to') but state no durable fact about the user's training.
    The only admissible output is the stated wish for age-aware
    recommendations, which the trip turn does state.
    """

    turns = [
        "Here is my sourdough recipe. What would be the best modification to improve that?",
        "I am planning a trip to Japan. Are you factoring my age into this recommendation?",
        "I did the dishes for an hour.",
        "I did the laundry for the whole family yesterday.",
        "I cannot open this PDF, can you help?",
        "Our meeting is in 10 minutes.",
        "I want to know what time the meeting starts today.",
        "Use the search tool for this question.",
        "I keep getting a 500 error in production.",
        "All tests must pass before you answer.",
    ]
    events = [
        _personal_agent_event().model_copy(
            update={"id": sequence, "sequence": sequence, "payload": {"content": turn}}
        )
        for sequence, turn in enumerate(turns, start=7)
    ]

    candidates = await formation.HighRecallCandidateExtractor().extract(
        events, principal=principal(), scope="general"
    )

    statements = [candidate.statement for candidate in candidates]
    assert not any("5x5" in statement or "strength" in statement for statement in statements)
    assert statements == ["User wants recommendations that account for their age."], statements


def test_fallback_episode_holds_any_single_owned_event() -> None:
    """An oversized event forms its own segment, so its lossless episode must fit.

    The API accepts a one-mebibyte body and segmentation never splits an event,
    so the narrative bound must hold that event plus a citation prefix per line;
    a forty-kilobyte turn once raised an uncaught validation error here and
    crashed the whole consolidation.
    """

    from agent_core.api.middleware import MAX_BODY_BYTES
    from agent_core.domain.memory import INTEGRATED_EPISODE_NARRATIVE_MAX_LENGTH
    from agent_core.memory.distillation import MAX_SEGMENT_SOURCE_EVENTS

    assert (
        MAX_BODY_BYTES + MAX_SEGMENT_SOURCE_EVENTS * len("[e:18446744073709551615] \n")
    ) <= INTEGRATED_EPISODE_NARRATIVE_MAX_LENGTH
    event = _personal_agent_event().model_copy(update={"payload": {"content": "x" * 40_000}})
    [segment] = plan_segments([event])

    episode = deterministic_integrated_episode(
        segment, principal=principal(), episode_id=UUID(int=9), created_at=NOW
    )

    assert episode.narrative == f"[e:7] {'x' * 40_000}"


async def test_oversized_event_completes_through_the_audited_fallback() -> None:
    extractor, _provider, factory = await _distillation_extractor(["not-json"] * 3)
    await user_event(factory, "x" * 40_000)

    result = await extractor.extract(
        await session_events(factory), principal=principal(), scope="general"
    )

    assert list(result) == []
    assert extractor.last_audit.segment_count == 1
    assert extractor.last_audit.episode_count == 1


async def test_fallback_resolves_a_pronoun_only_to_the_adjacent_clause() -> None:
    """ "That" after an unrecognized clause refers to that clause, not an earlier one."""

    ambiguous = (
        "I swim every week. Here is my sourdough recipe. "
        "What would be the best modification to improve that?"
    )
    adjacent = "I swim every week. What would be the best modification to improve that?"

    def turn(text: str) -> EventEnvelope:
        return _personal_agent_event().model_copy(update={"payload": {"content": text}})

    ambiguous_candidates = await formation.HighRecallCandidateExtractor().extract(
        [turn(ambiguous)], principal=principal(), scope="general"
    )
    adjacent_candidates = await formation.HighRecallCandidateExtractor().extract(
        [turn(adjacent)], principal=principal(), scope="general"
    )

    assert [candidate.statement for candidate in ambiguous_candidates] == ["User swims every week."]
    assert "User wants to improve their swimming." in {
        candidate.statement for candidate in adjacent_candidates
    }


def test_semantic_duplicate_detection_compares_assertions_not_only_subjects() -> None:
    """Two candidates about one subject are one memory only when they say one thing."""

    red = _candidate(
        subject="BMW",
        statement="User's BMW is red.",
        claim_kind="resource",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "my BMW is red"}],
    )
    tires = red.model_copy(update={"statement": "User's BMW needs new tires."})
    can_meet = _candidate(
        subject="Friday meetings",
        statement="User can take meetings on Fridays.",
        claim_kind="constraint",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "meetings on Fridays"}],
    )
    cannot_meet = can_meet.model_copy(update={"statement": "User cannot take meetings on Fridays."})
    hundred = _candidate(
        subject="User",
        statement="User ran 100 miles in training last month.",
        claim_kind="habit",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "ran 100 miles in training last month"}],
    )
    two_hundred = hundred.model_copy(
        update={"statement": "User ran 200 miles in training last month."}
    )
    tea = _candidate(
        subject="hot drinks",
        statement="User prefers tea to coffee.",
        claim_kind="preference",
        source_event_ids=[9],
        evidence_spans=[{"source_event_id": 9, "text": "prefer tea to coffee"}],
    )
    coffee = tea.model_copy(
        update={"subject": "beverage preference", "statement": "User prefers coffee to tea."}
    )

    # One subject and claim kind is one conflict key, so wording alone merges...
    assert _candidates_semantically_duplicate(red, tires)
    # ...but a contradiction never does; consolidation resolves it instead.
    assert not _candidates_semantically_duplicate(can_meet, cannot_meet)
    assert not _candidates_semantically_duplicate(hundred, two_hundred)
    assert not _candidates_semantically_duplicate(tea, coffee)
    assert _candidates_semantically_duplicate(
        red, red.model_copy(update={"statement": "The user's BMW is red."})
    )


async def test_high_recall_fallback_survives_a_long_unterminated_turn() -> None:
    """A long message must not crash consolidation and stays inside every bound."""

    unterminated = "I want to " + "keep strength as I age and stay fit " * 400
    repeated = "the blue theme works better for me " * 3000
    events = [
        _personal_agent_event().model_copy(
            update={"id": 7, "sequence": 7, "payload": {"content": unterminated}}
        ),
        _personal_agent_event().model_copy(
            update={"id": 8, "sequence": 8, "payload": {"content": repeated}}
        ),
    ]

    candidates = await formation.HighRecallCandidateExtractor().extract(
        events, principal=principal(), scope="general"
    )

    assert len(repeated) > 100_000
    # Every pattern scans one bounded clause at a time, never the whole turn:
    # the unterminated turn is a single clause and the repeated turn splits into
    # nothing longer than the clause bound the patterns are written against.
    assert len(formation.split_source_clauses(unterminated)) == 1
    assert all(len(clause) <= len(repeated) for clause in formation.split_source_clauses(repeated))
    for candidate in candidates:
        assert len(candidate.statement) <= 8192
        assert len(candidate.subject) <= 512
        assert all(len(span.text) <= 8192 for span in candidate.evidence_spans)


async def test_correction_veto_is_clause_scoped() -> None:
    """A retraction suppresses its own clause, not the unrelated claims beside it."""

    events = [
        _personal_agent_event().model_copy(
            update={
                "id": 7,
                "sequence": 7,
                "payload": {"content": "I don't drive. I keep tax records in the Blue folder."},
            }
        ),
        _personal_agent_event().model_copy(
            update={
                "id": 8,
                "sequence": 8,
                "payload": {"content": "I no longer live in Rome. I lead the infrastructure team."},
            }
        ),
    ]

    candidates = await formation.HighRecallCandidateExtractor().extract(
        events, principal=principal(), scope="general"
    )

    statements = {candidate.statement for candidate in candidates}
    assert "User keeps tax records in the Blue folder." in statements
    assert "User leads an infrastructure team." in statements
    assert not any("drive" in statement or "Rome" in statement for statement in statements)


async def test_legacy_candidates_cite_their_own_clause() -> None:
    """Evidence precision survives the high-recall wrapper."""

    events = [
        _personal_agent_event().model_copy(
            update={
                "id": 7,
                "sequence": 7,
                "payload": {
                    "content": (
                        "My daughter starts college next year and I prefer concise answers. "
                        "Remember that the deploy gate is manual."
                    )
                },
            }
        )
    ]

    candidates = await formation.HighRecallCandidateExtractor().extract(
        events, principal=principal(), scope="general"
    )

    preference = next(
        candidate
        for candidate in candidates
        if candidate.statement == "User prefers concise answers."
    )
    assert [span.text for span in preference.evidence_spans] == ["I prefer concise answers"]


def test_source_clauses_keep_paths_versions_and_abbreviations() -> None:
    assert formation.split_source_clauses(
        "Our runbook is in docs/operations.md. We use Python 3.12, e.g. for the API. "
        "I swim, run, or bike most days and I lift weights twice a week!"
    ) == [
        "Our runbook is in docs/operations.md",
        "We use Python 3.12, e.g. for the API",
        "I swim, run, or bike most days",
        "I lift weights twice a week",
    ]


async def test_distillation_grounds_evidence_that_contains_a_period() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "Our runbook is in docs/operations.md.")
    provider._script.turns = [
        ScriptedTurn(
            text=_scripted_episode([source_id], ["Our runbook is in docs/operations.md."])
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "subject": "operations runbook",
                            "statement": "The user's runbook is in docs/operations.md.",
                            "source_event_ids": [source_id],
                            "sensitivity_guess": "internal",
                            "claim_kind": "resource",
                            "derivation": "direct",
                            "evidence_spans": [
                                {
                                    "source_event_id": source_id,
                                    "text": "runbook is in docs/operations.md",
                                }
                            ],
                        }
                    ],
                    "coverage": [
                        {
                            "coverage_unit_id": f"{source_id}:1",
                            "decision": "formed",
                            "candidate_indexes": [0],
                            "prediction_indexes": [],
                        }
                    ],
                }
            )
        ),
    ]

    await extractor.extract(await session_events(factory), principal=principal(), scope="general")

    audit = extractor.last_audit
    assert audit.fallback_stages == []
    assert audit.provider_candidates == 1
    assert audit.rejected_provider_candidates == 0
    assert audit.coverage_dispositions == {f"{source_id}:1": "formed"}


async def test_represented_coverage_is_verified_against_the_cited_memory() -> None:
    """A clause is only represented by a memory that asserts it."""

    clock, factory, baseline, _retriever = await formation_stack()
    await user_event(factory, "I prefer concise answers.")
    existing = await baseline.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers concise answers.",
        subject="answer style",
        scope="general",
        belief_type=BeliefType.PREFERENCE,
    )
    new_source = await user_event(factory, "I swim three mornings each week.")
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=_scripted_episode([new_source], ["I swim three mornings each week."])
                ),
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "predictions": [
                                {
                                    "episode_index": 0,
                                    "statement": "User prefers concise answers.",
                                    "attributed_memory_ids": [str(existing.id)],
                                }
                            ]
                        }
                    )
                ),
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "candidates": [],
                            "coverage": [
                                {
                                    "coverage_unit_id": f"{new_source}:1",
                                    "decision": "represented",
                                    "candidate_indexes": [],
                                    "prediction_indexes": [0],
                                }
                            ],
                        }
                    )
                ),
            ]
        ),
        clock,
    )
    extractor = NemoriAssistedCandidateExtractor(
        provider=provider,
        resolved_model=_resolved_model(),
        uow_factory=factory,
        clock=clock,
        ids=baseline._ids,
    )
    new_events = [event for event in await session_events(factory) if event.sequence == new_source]

    await extractor.extract(new_events, principal=principal(), scope="general")

    audit = extractor.last_audit
    assert "prediction_error_distillation" not in audit.fallback_stages
    assert audit.represented_unverified == 1
    assert audit.coverage_dispositions == {f"{new_source}:1": "represented_unverified"}
    assert audit.coverage_counts == {"represented": 1, "represented_unverified": 1}
    assert audit.provider_stage_metrics["prediction_error_distillation"]["outcome"] == (
        "partial_validation"
    )


def test_anticipation_prefix_is_bounded_to_the_most_recent_text() -> None:
    """The blinded prefix keeps the latest text under a byte bound, never the whole session."""

    from agent_core.memory.distillation import MAX_ANTICIPATION_PREFIX_BYTES

    sizes = [100_000, 100_000, 50_000]
    prefix_events = [
        _personal_agent_event().model_copy(
            update={"id": sequence, "sequence": sequence, "payload": {"content": "x" * size}}
        )
        for sequence, size in enumerate(sizes, start=1)
    ]
    current = _personal_agent_event().model_copy(update={"id": 4, "sequence": 4})
    episode = deterministic_integrated_episode(
        [current], principal=principal(), episode_id=UUID(int=9), created_at=NOW
    )

    prompt = json.loads(
        NemoriAssistedCandidateExtractor._anticipation_prompt(
            [*prefix_events, current], [episode], []
        )
    )

    assert sizes[1] + sizes[2] <= MAX_ANTICIPATION_PREFIX_BYTES < sum(sizes)
    assert [event["source_event_id"] for event in prompt["prefix_events"]] == [2, 3]
    assert prompt["episode_cues"] == [{"episode_index": 0, "before_event_sequence": 4}]


async def test_long_batches_are_segmented_into_bounded_three_call_rounds() -> None:
    """A batch beyond one ledger's worth of clauses runs three calls per segment."""

    extractor, provider, factory = await _distillation_extractor([])
    texts = [f"I keep item {index} in the Blue folder." for index in range(100)]
    source_ids = [await user_event(factory, text) for text in texts]
    segments = plan_segments(await session_events(factory))
    assert [len(segment) for segment in segments] == [90, 10]
    turns: list[ScriptedTurn] = []
    for segment in segments:
        segment_ids = [event.sequence for event in segment]
        segment_texts = [texts[source_ids.index(source_id)] for source_id in segment_ids]
        turns.extend(
            [
                ScriptedTurn(text=_scripted_episode(segment_ids, segment_texts)),
                ScriptedTurn(text='{"predictions":[]}'),
                ScriptedTurn(text=_empty_distillation(*segment_ids)),
            ]
        )
    provider._script.turns = turns

    await extractor.extract(await session_events(factory), principal=principal(), scope="general")

    audit = extractor.last_audit
    assert audit.segment_count == 2
    assert audit.provider_calls == 6
    assert audit.fallback_stages == []
    assert audit.episode_count == 2
    assert set(audit.provider_stage_metrics) == {
        "episode_integration",
        "anticipation",
        "prediction_error_distillation",
        "episode_integration#2",
        "anticipation#2",
        "prediction_error_distillation#2",
    }
    assert audit.coverage_counts == {"not_memory": 100}


def test_distillation_output_ceiling_fits_a_full_segment_ledger() -> None:
    """Ninety clauses of dispositions plus one candidate each stay inside the cap."""

    needed = MAX_SEGMENT_COVERAGE_UNITS * (30 + 110)
    assert needed <= 0.8 * DISTILLATION_MAXIMUM_OUTPUT_TOKENS


def test_comparative_scorer_rejects_supersets_negations_counts_and_siblings() -> None:
    """The scorer that authorizes activation must say no to a wrong memory."""

    pairs = [
        ("User has a son named Robert who lives in Berlin.", "User has a son."),
        ("User can take meetings on Fridays.", "User cannot take meetings on Fridays."),
        ("User has at least one sister.", "User has at least two sisters."),
        (
            "User regularly runs on non-strength-training days.",
            "User regularly swims on non-strength-training days.",
        ),
        (
            "User's current 5x5 progress has stalled.",
            "User's current 5x5 progress has not stalled.",
        ),
    ]
    for candidate, reference in pairs:
        assert not statements_equivalent(candidate, reference), (candidate, reference)
        assert not statements_equivalent(reference, candidate), (reference, candidate)
    assert statements_equivalent(
        "User's goal is to finish the marathon.", "User wants to finish the marathon."
    )
    assert not subject_matches("User", ["marathon"])
    case = MemoryDistillationCase.model_validate(
        {
            "id": "goal-gate-901",
            "label": "must_form",
            "scenario": "ordinary",
            "events": [{"actor": "user", "text": "My goal is to finish the marathon."}],
            "expected": [
                {
                    "claim_kind": "goal",
                    "derivation": "direct",
                    "longevity": "ongoing",
                    "subjects": ["marathon"],
                    "statements": ["User wants to finish the marathon."],
                    "evidence_text": ["finish the marathon"],
                }
            ],
        }
    )
    generic = DistillationEvaluationBelief(
        claim_kind=MemoryClaimKind.GOAL,
        derivation=MemoryDerivation.DIRECT,
        longevity=MemoryLongevity.ONGOING,
        subject="User",
        statement="User wants to finish the marathon.",
    )
    assert score_distillation_case(case, [generic]).matched == 0


def test_formation_corpus_v3_runs_against_a_populated_store() -> None:
    root = Path(__file__).resolve().parents[2]
    corpus, _digest = load_distillation_corpus(root)

    pool = corpus.seed_pools["populated"]
    assert len(pool) >= 25
    assert {seed.claim_kind for seed in pool} == set(MemoryClaimKind)
    seeded = [case for case in corpus.cases if case.prior_beliefs_pool == "populated"]
    assert any(case.scenario == "rich-conversation" for case in seeded)
    assert sum(sum(event.actor == "user" for event in case.events) >= 2 for case in seeded) >= 2
    assert all(case.label != "must_not_form" for case in seeded)
    # A populated store must also be asked about: some seeded case restates a
    # seed, spans two segments so the blinded prefix carries a cue, and the
    # provider has to mark that clause represented by the seed that asserts it.
    represented = [case for case in seeded if case.represented_text]
    assert represented, "no seeded case labels a clause a seed represents"
    for case in represented:
        assert len(plan_segments(_corpus_events(case))) >= 2, case.id
        for text in case.represented_text:
            assert any(statement_supports_clause(seed.statement, text) for seed in pool), text


def _corpus_events(case: MemoryDistillationCase) -> list[EventEnvelope]:
    return [
        _personal_agent_event().model_copy(
            update={"id": sequence, "sequence": sequence, "payload": {"content": event.text}}
        )
        for sequence, event in enumerate(case.events, start=1)
        if event.actor == "user"
    ]


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (
            "The user coordinates volunteers at the food bank.",
            "User coordinates volunteers at the food bank.",
        ),
        ("the user practices the piano most evenings", "User practices the piano most evenings."),
        ("the user's class app is being prototyped.", "The user's class app is being prototyped."),
        ("user prefers examples before theory.", "User prefers examples before theory."),
    ],
)
def test_local_policy_folds_the_user_article_instead_of_rejecting(
    statement: str, expected: str
) -> None:
    """A good claim is not discarded over the article in front of its subject."""

    candidate = _candidate(
        claim_kind="preference",
        derivation="direct",
        longevity="durable",
        subject="explanation style",
        statement=statement,
        evidence_spans=[{"source_event_id": 7, "text": "building a personal AI agent"}],
    )

    normalized = _normalize_provider_candidate(
        candidate,
        by_sequence={7: _personal_agent_event()},
        scope="general",
    )

    assert normalized.statement == expected


def test_combiner_merges_synonym_subjects_and_keeps_the_direct_claim() -> None:
    """One claim stays one memory across subject wording and derivation."""

    biking = _candidate(
        claim_kind="habit",
        derivation="direct",
        longevity="ongoing",
        subject="biking",
        statement="User bikes on the rest of the days.",
        evidence_spans=[{"source_event_id": 7, "text": "building a personal AI agent"}],
    )
    cycling = biking.model_copy(
        update={
            "subject": "Cycling",
            "statement": "User bikes on some days when not doing the 5x5 strength routine.",
        }
    )
    assert _candidates_semantically_duplicate(biking, cycling)

    direct = _candidate(
        claim_kind="preference",
        derivation="direct",
        longevity="durable",
        subject="age-aware recommendations",
        statement="User wants recommendations that account for their age.",
        evidence_spans=[{"source_event_id": 7, "text": "building a personal AI agent"}],
    )
    guess = direct.model_copy(
        update={
            "subject": "age-related recommendations",
            "statement": "User likely prefers recommendations that account for their age.",
            "derivation": "hypothesis",
            "longevity": "tentative",
            "model_confidence": 0.35,
        }
    )
    assert _candidates_semantically_duplicate(direct, guess)
    unrelated = direct.model_copy(
        update={"subject": "swimming", "statement": "User swims on the rest of the days."}
    )
    assert not _candidates_semantically_duplicate(biking, unrelated)


async def test_distillation_prompt_carries_no_owner_identifiers() -> None:
    """The final call sees narrative, subjects, and source ids, never identity."""

    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am building a personal AI agent.")
    provider._script.turns = [
        *provider._script.turns,
        ScriptedTurn(text=_scripted_episode([source_id], ["I am building a personal AI agent."])),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(text=_empty_distillation(source_id)),
    ]

    await extractor.extract(await session_events(factory), principal=principal(), scope="general")

    prompt = provider.requests[2].conversation[1]
    assert isinstance(prompt, UserMessage)
    part = prompt.content[0]
    assert isinstance(part, TextPart)
    payload = json.loads(part.text)
    assert set(payload["episodes"][0]) == {
        "episode_index",
        "narrative",
        "subjects",
        "source_event_ids",
    }
    assert TENANT not in part.text
    assert PRINCIPAL_ID not in part.text
    assert str(SESSION_ID) not in part.text
    assert "derivation_key" not in part.text


async def test_anticipation_prefix_stops_before_the_earliest_episode() -> None:
    """With several episodes in one request, no episode's evidence is a cue.

    The prefix is the batch text before the earliest episode; an earlier
    episode's evidence must not reach the request as a later episode's cue,
    and within one segment that means the prefix is only what precedes it.
    """

    _clock, factory, _baseline, _retriever = await formation_stack()
    earlier = await user_event(factory, "EARLIER_CONTEXT I keep tax records in the Blue folder.")
    first = await user_event(factory, "FIRST_EPISODE I swim three mornings each week.")
    second = await user_event(factory, "SECOND_EPISODE I moved to Portland last month.")
    events = await session_events(factory)
    by_sequence = {event.sequence: event for event in events}
    episodes = [
        deterministic_integrated_episode(
            [by_sequence[sequence]],
            principal=principal(),
            episode_id=UUID(int=910 + index),
            created_at=NOW,
        )
        for index, sequence in enumerate((first, second))
    ]

    prompt = NemoriAssistedCandidateExtractor._anticipation_prompt(events, episodes, [])
    payload = json.loads(prompt)

    assert [item["source_event_id"] for item in payload["prefix_events"]] == [earlier]
    assert payload["episode_cues"] == [
        {"before_event_sequence": first, "episode_index": 0},
        {"before_event_sequence": second, "episode_index": 1},
    ]
    assert "EARLIER_CONTEXT" in prompt
    assert "FIRST_EPISODE" not in prompt
    assert "SECOND_EPISODE" not in prompt


async def test_anticipation_never_sends_sensitive_or_restricted_memories() -> None:
    """Provider egress carries only beliefs at or below internal sensitivity."""

    clock, factory, baseline, _retriever = await formation_stack()
    await user_event(factory, "I prefer concise answers.")
    await baseline.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers concise answers.",
        subject="answer style",
        scope="general",
        belief_type=BeliefType.PREFERENCE,
        sensitivity=Sensitivity.INTERNAL,
    )
    await user_event(factory, "My daughter starts college next year.")
    await baseline.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User has a daughter named DAUGHTER_SENTINEL.",
        subject="daughter",
        scope="general",
        belief_type=BeliefType.RELATIONSHIP,
        sensitivity=Sensitivity.SENSITIVE,
    )
    await user_event(factory, "My passport number is on file.")
    await baseline.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User's passport is RESTRICTED_SENTINEL.",
        subject="passport",
        scope="general",
        belief_type=BeliefType.FACT,
        sensitivity=Sensitivity.RESTRICTED,
    )
    new_source = await user_event(factory, "I swim three mornings each week.")
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=_scripted_episode([new_source], ["I swim three mornings each week."])
                ),
                ScriptedTurn(text='{"predictions":[]}'),
                ScriptedTurn(text=_empty_distillation(new_source)),
            ]
        ),
        clock,
    )
    extractor = NemoriAssistedCandidateExtractor(
        provider=provider,
        resolved_model=_resolved_model(),
        uow_factory=factory,
        clock=clock,
        ids=baseline._ids,
    )
    new_events = [event for event in await session_events(factory) if event.sequence == new_source]

    await extractor.extract(new_events, principal=principal(), scope="general")

    anticipation = provider.requests[1].conversation[1]
    assert isinstance(anticipation, UserMessage)
    part = anticipation.content[0]
    assert isinstance(part, TextPart)
    assert "User prefers concise answers." in part.text
    assert "DAUGHTER_SENTINEL" not in part.text
    assert "RESTRICTED_SENTINEL" not in part.text
    for request in provider.requests:
        for message in request.conversation:
            for content in getattr(message, "content", []):
                if isinstance(content, TextPart):
                    assert "DAUGHTER_SENTINEL" not in content.text
                    assert "RESTRICTED_SENTINEL" not in content.text


def _provider_evidence(
    formation_policy_version: str,
    *,
    policy_version: str = "default@test",
    build_ref: str = "test-build",
    model_policy: str = "fake",
) -> ProviderExtractionEvaluationEvidence:
    repaired = formation_policy_version == REPAIRED_PROVIDER_FORMATION_POLICY_VERSION
    return ProviderExtractionEvaluationEvidence(
        extractor_version=(
            REPAIRED_PROVIDER_EXTRACTOR_VERSION if repaired else "provider-assisted-v2"
        ),
        formation_policy_version=formation_policy_version,
        model_policy=model_policy,
        provider="fake",
        model="scripted",
        policy_profile="default",
        policy_version=policy_version,
        build_ref=build_ref,
        corpus_sha256="a" * 64,
        sample_count=25,
        positive_case_count=21,
        minimum_supported_case_count=17,
        deterministic_supported_case_count=9,
        provider_supported_case_count=18,
        deterministic_supported_candidates=12,
        provider_supported_candidates=20,
        deterministic_fabricated_candidates=0,
        provider_fabricated_candidates=0,
        deterministic_policy_failures=0,
        provider_policy_failures=0,
        seeded_case_count=21 if repaired else 0,
        evaluated_at=NOW,
    )


def _populated_store_response(source: int) -> str:
    return json.dumps(
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


async def test_repaired_provider_policy_never_starves_on_a_populated_store() -> None:
    """gate.memory.provider_budget_repaired.

    On a store of sixty beliefs the frozen formation@8 control cannot afford one
    output token and skips its call; formation@10 makes the call with the full
    output ceiling, ranks its compact belief view by relevance to the batch
    instead of recency, and sends only public or internal beliefs.
    """

    clock, factory, service, _retriever = await formation_stack()
    seed = await user_event(factory, "Some earlier context the seeded beliefs cite.")
    # Oldest first: recency alone would push the relevant belief out of a
    # fifty-belief window, and the sensitive beliefs must never leave the store.
    await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User's daughter started an astronomy club at her school last spring.",
        subject="astronomy club",
        scope="project-a",
        source_event_ids=[seed],
    )
    for index in range(3):
        await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement=f"User keeps the private ledger number {index} for the astronomy club.",
            subject=f"private ledger {index}",
            scope="project-a",
            sensitivity=Sensitivity.SENSITIVE,
            source_event_ids=[seed],
        )
    for index in range(56):
        await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement=f"User files maintenance report {index} in the shared operations folder.",
            subject=f"maintenance report {index}",
            scope="project-a",
            source_event_ids=[seed],
        )
    source = await user_event(factory, "The astronomy club was my daughter's idea.")
    resolved = ResolvedModel(
        provider="fake",
        model="scripted",
        policy_name="fake",
        pricing=ModelPricing(input_per_mtok=Decimal("5.00"), output_per_mtok=Decimal("30.00")),
        limits=ModelLimits(context_window_tokens=200_000, max_output_tokens=128_000),
        resolved_at=NOW,
    )

    def extractor(
        policy: str, provider: FakeModelProvider, first_id: int
    ) -> ProviderAssistedCandidateExtractor:
        return ProviderAssistedCandidateExtractor(
            provider=provider,
            resolved_model=resolved,
            uow_factory=factory,
            clock=clock,
            ids=SequenceIdFactory(UUID(int=value) for value in range(first_id, first_id + 100)),
            principal=principal(),
            agent_id=AGENT_ID,
            agent_version="1.0.0",
            policy_profile="default",
            policy_version="default@test",
            evidence=_provider_evidence(policy),
            fallback=formation.DeterministicCandidateExtractor(),
            formation_policy_version=policy,
        )

    frozen_provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text=_populated_store_response(source))]), clock
    )
    await extractor(PROVIDER_FORMATION_POLICY_VERSION, frozen_provider, 7_000).extract(
        await session_events(factory), principal=principal(), scope="project-a"
    )
    assert frozen_provider.requests == []
    async with factory() as uow:
        failed = await uow.process_events.list("memory.provider_extraction.failed")
    assert [audit.payload["outcome"] for audit in failed] == ["cost_budget_exceeded"]

    repaired_provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text=_populated_store_response(source))]), clock
    )
    candidates = await extractor(
        REPAIRED_PROVIDER_FORMATION_POLICY_VERSION, repaired_provider, 7_200
    ).extract(await session_events(factory), principal=principal(), scope="project-a")

    assert [(item.subject, item.statement) for item in candidates] == [
        ("daughter", "User has at least one daughter.")
    ]
    request = repaired_provider.requests[0]
    assert request.maximum_output_tokens == 16_384
    assert request.metadata["formation_policy_version"] == "formation@10"
    user_message = request.conversation[1]
    assert isinstance(user_message, UserMessage)
    text_part = user_message.content[0]
    assert isinstance(text_part, TextPart)
    prompt = text_part.text
    payload = json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])
    related = payload["related_beliefs"]
    assert 1 <= len(related) <= 50
    assert related[0]["subject"] == "astronomy club"
    assert all("private ledger" not in belief["statement"] for belief in related)
    assert all("private ledger" not in belief["subject"] for belief in related)
    async with factory() as uow:
        completed = await uow.process_events.list("memory.provider_extraction.completed")
    assert len(completed) == 1
    audit = completed[0].payload
    assert audit["formation_policy_version"] == "formation@10"
    assert audit["extractor_version"] == REPAIRED_PROVIDER_EXTRACTOR_VERSION
    assert audit["budget"]["maximum_output_tokens"] == 16_384
    assert audit["selected_source_event_ids"] == [seed, source]
    assert "private ledger" not in json.dumps(audit)


def _runtime_policy_version() -> str:
    settings = memory_settings()
    return load_ruleset_documents(
        load_config_document(settings, "policy/default.yaml"),
        load_config_document(settings, "policy/hardline.yaml"),
    ).policy_version


async def _select_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    policies: tuple[str, ...],
    pin: MemoryFormationPolicyPin | None,
) -> dict[str, object]:
    release_root = tmp_path / "release-evidence"
    release_root.mkdir(parents=True, exist_ok=True)
    for index, policy in enumerate(policies):
        if policy == "formation@9":
            artifact = MemoryDistillationEvidence(
                model_policy="fake-balanced",
                provider="fake",
                model="scripted",
                policy_profile="default",
                policy_version=_runtime_policy_version(),
                scorer_version="distillation-scorer@3",
                build_ref="9" * 40,
                corpus_sha256="b" * 64,
                sample_count=61,
                positive_case_count=49,
                seeded_case_count=5,
                represented_case_count=1,
                direct_must_form_recall=1,
                hypothesis_must_form_recall=1,
                benign_precision=0.96,
                useful_recall_lift_percentage_points=90,
                correction_rate_per_hundred=0,
                evidence_disposition_precision=0.9,
                provider_calls_per_segment=3,
                provider_calls_measured=183,
                consolidations_measured=61,
                provider_cost_usd="2.5",
                evaluated_at=NOW,
            ).model_dump_json()
        else:
            artifact = _provider_evidence(
                policy,
                policy_version=_runtime_policy_version(),
                build_ref=policy.replace("@", "-"),
                model_policy="fake-balanced",
            ).model_dump_json()
        (release_root / f"{index}-{policy.replace('@', '')}.json").write_text(
            artifact, encoding="utf-8"
        )
    monkeypatch.setattr(config_module, "PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT", release_root)
    settings = replace(
        memory_settings(),
        memory_provider_extraction_mode=MemoryProviderExtractionMode.AUTO,
        memory_formation_policy_pin=pin,
        artifact_root=tmp_path / "artifacts",
    )
    async with build(settings=settings, storage="memory") as app, app.uow_factory() as uow:
        selections = await uow.process_events.list("memory.provider_extraction.selection")
    assert len(selections) == 1
    return dict(selections[0].payload)


async def test_automatic_selection_prefers_the_newest_evidenced_policy_and_honors_a_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gate.memory.provider_policy_precedence.

    With every artifact present automatic selection activates formation@9;
    without it, formation@10 outranks the frozen formation@8 control; a pin
    holds any one of the three; and a pin to an unevidenced policy falls back
    deterministically with a content-free audit rather than activating another.
    """

    everything = ("formation@8", "formation@10", "formation@9")
    newest = await _select_with(monkeypatch, tmp_path / "a", policies=everything, pin=None)
    assert (newest["outcome"], newest["formation_policy_version"]) == ("activated", "formation@9")

    repaired = await _select_with(
        monkeypatch, tmp_path / "b", policies=("formation@8", "formation@10"), pin=None
    )
    assert (repaired["outcome"], repaired["formation_policy_version"]) == (
        "activated",
        "formation@10",
    )
    assert repaired["evidence_build_ref"] == "formation-10"

    for pin, expected in (
        (MemoryFormationPolicyPin.PROVIDER_ASSISTED, "formation@8"),
        (MemoryFormationPolicyPin.REPAIRED_PROVIDER_ASSISTED, "formation@10"),
        (MemoryFormationPolicyPin.DISTILLATION, "formation@9"),
    ):
        pinned = await _select_with(monkeypatch, tmp_path / pin.value, policies=everything, pin=pin)
        assert (pinned["outcome"], pinned["formation_policy_version"]) == ("activated", expected)
        assert pinned["policy_pin"] == pin.value

    unevidenced = await _select_with(
        monkeypatch,
        tmp_path / "d",
        policies=("formation@8", "formation@9"),
        pin=MemoryFormationPolicyPin.REPAIRED_PROVIDER_ASSISTED,
    )
    assert unevidenced["outcome"] == "deterministic_fallback"
    assert unevidenced["reason"] == "pinned_policy_unevidenced"
    assert unevidenced["formation_policy_version"] == "formation@7"
