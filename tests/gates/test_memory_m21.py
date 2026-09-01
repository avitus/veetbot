"""Milestone 21 adaptive memory-distillation gates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.persistence.memory_repositories import _memory_values
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
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
    RejectionKind,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ResolvedModel,
    ScriptedTurn,
    SystemMessage,
    TextPart,
    UserMessage,
)
from agent_core.domain.views import MemoryView
from agent_core.evals.memory_distillation import load_distillation_corpus
from agent_core.memory import SHIPPED_MEMORY_CANDIDATE_EXTRACTORS, formation
from agent_core.memory.distillation import (
    NemoriAssistedCandidateExtractor,
    _normalize_provider_candidate,
    deterministic_integrated_episode,
    select_distillation_policy,
    validate_integrated_episode,
)
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.model_extraction import ModelAssistedCandidateExtractor
from agent_core.memory.provider_extraction import (
    PROVIDER_FORMATION_POLICY_VERSION,
    ProviderAssistedCandidateExtractor,
)
from agent_core.memory.retrieval import _score, render_memory
from tests.contract.memory_fixtures import (
    formation_stack,
    memory,
    recall_query,
    session_events,
    user_event,
)
from tests.contract.support import NOW, PRINCIPAL_ID, SESSION_ID, principal


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


async def test_one_consolidation_makes_exactly_three_batched_calls() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am building a personal AI agent.")
    responses = [
        json.dumps(
            {
                "narrative": f"[e:{source_id}] I am building a personal AI agent.",
                "subjects": ["personal AI agent"],
                "source_event_ids": [source_id],
            }
        ),
        '{"predictions":[]}',
        '{"candidates":[]}',
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


async def test_distillation_normalizes_provider_policy_fields_locally() -> None:
    extractor, provider, factory = await _distillation_extractor([])
    source_id = await user_event(factory, "I am prototyping an app for my class.")
    provider._script.turns = [
        ScriptedTurn(
            text=json.dumps(
                {
                    "narrative": f"[e:{source_id}] I am prototyping an app for my class.",
                    "subjects": ["software-development experience"],
                    "source_event_ids": [source_id],
                }
            )
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "belief_type": "fact",
                            "subject": "software-development experience",
                            "statement": "User may have software-development experience.",
                            "source_event_ids": [source_id],
                            "model_confidence": 0.99,
                            "proposed_scope": "general",
                            "proposed_portability": "local",
                            "sensitivity_guess": "public",
                            "claim_kind": "skill",
                            "derivation": "hypothesis",
                            "longevity": "ongoing",
                            "evidence_spans": [
                                {
                                    "source_event_id": source_id,
                                    "text": "prototyping an app",
                                }
                            ],
                            "valid_from": (NOW + timedelta(days=365)).isoformat(),
                            "expires_hint": (NOW + timedelta(days=730)).isoformat(),
                        }
                    ]
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
    assert skill.valid_from == NOW
    assert skill.expires_hint is None


def test_provider_cannot_render_a_hypothesis_as_an_unqualified_fact() -> None:
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
                    "narrative": (
                        f"[e:{source_id}] CURRENT_EVIDENCE_SENTINEL I am building a "
                        "personal AI agent."
                    ),
                    "subjects": [],
                    "source_event_ids": [source_id],
                }
            )
        ),
        ScriptedTurn(text='{"predictions":[]}'),
        ScriptedTurn(text='{"candidates":[]}'),
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
    assert set(json.loads(payload)) == {"cue", "prior_memories"}


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


async def test_provider_fallback_completes_without_scheduling_legacy_retries() -> None:
    extractor, provider, factory = await _distillation_extractor(
        ["not-json", "also-not-json", "still-not-json"]
    )
    await user_event(factory, "I am building a personal AI agent.")
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

    assert len(provider.requests) == 3
    assert result.run.watermark_after == 1
    assert result.run.committed == 2
    assert result.run.fallback_stages == [
        "episode_integration",
        "anticipation",
        "prediction_error_distillation",
    ]
    assert not any(
        event.event_type == "memory.formation.requested"
        and event.payload.get("trigger") == "provider_retry"
        for event in await session_events(factory)
    )


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
                    "narrative": f"[e:{source_id}] I am building a personal AI agent.",
                    "subjects": [],
                    "source_event_ids": [source_id],
                }
            )
        ),
        ScriptedTurn(
            text=(
                '{"predictions":[{"statement":"User is building a personal AI '
                'agent.","attributed_memory_ids":[]}]}'
            )
        ),
        ScriptedTurn(text='{"candidates":[]}'),
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
                            "narrative": (f"[e:{new_source}] I am building a personal AI agent."),
                            "subjects": [],
                            "source_event_ids": [new_source],
                        }
                    )
                ),
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "predictions": [
                                {
                                    "statement": "User is building a personal AI agent.",
                                    "attributed_memory_ids": [str(prior.id)],
                                }
                            ]
                        }
                    )
                ),
                ScriptedTurn(text='{"candidates":[]}'),
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
        build_ref="build-123",
        corpus_sha256="a" * 64,
        sample_count=60,
        positive_case_count=48,
        direct_must_form_recall=0.96,
        hypothesis_must_form_recall=0.82,
        benign_precision=0.92,
        useful_recall_lift_percentage_points=18,
        correction_rate_per_hundred=4,
        provider_calls_per_consolidation=3,
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
        "provider_calls_per_consolidation": 2,
        "boundary_failures": 1,
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
    } <= {case.scenario for case in corpus.cases}
