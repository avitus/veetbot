"""Milestone 10 automatic memory-formation and lifecycle gates."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryCandidate,
    MemoryStatus,
    Polarity,
    Portability,
    Sensitivity,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.memory.formation import DeterministicCandidateExtractor, GovernedMemoryService
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.memory_fixtures import formation_stack, session_events, user_event
from tests.contract.support import AGENT_ID, NOW, SESSION_ID, TENANT, principal
from tests.integration.m2_support import memory_settings


class _ScriptedCandidateExtractor:
    name = "scripted-candidate-extractor"

    def __init__(self, candidates: list[MemoryCandidate]) -> None:
        self._candidates = candidates

    async def extract(
        self,
        events: list[EventEnvelope],
        *,
        principal: Principal,
        scope: str,
    ) -> list[MemoryCandidate]:
        del events, principal, scope
        return self._candidates


class _AdvancingCandidateExtractor:
    name = "advancing-candidate-extractor"

    def __init__(self, clock: FixedClock) -> None:
        self._clock = clock
        self._delegate = DeterministicCandidateExtractor()

    async def extract(
        self,
        events: list[EventEnvelope],
        *,
        principal: Principal,
        scope: str,
    ) -> list[MemoryCandidate]:
        self._clock.advance(timedelta(seconds=5))
        return await self._delegate.extract(events, principal=principal, scope=scope)


async def test_ordinary_conversation_forms_one_memory_per_durable_entity() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    source = await user_event(
        factory,
        "I have an Apple Watch and a BMW X3, and I prefer metric units.",
    )

    result = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )

    by_subject = {belief.subject: belief for belief in result.beliefs}
    assert set(by_subject) == {"Apple Watch", "BMW X3", "measurement units"}
    assert by_subject["Apple Watch"].statement == "User has an Apple Watch."
    assert by_subject["BMW X3"].statement == "User has a BMW X3."
    assert by_subject["measurement units"].statement == "User prefers metric units."
    assert by_subject["measurement units"].belief_type is BeliefType.PREFERENCE
    assert all(belief.source_event_ids == [source] for belief in by_subject.values())
    assert all(belief.formation_run_id == result.run.id for belief in by_subject.values())
    assert all(belief.authority is MemoryAuthority.INFERRED for belief in by_subject.values())
    assert all(belief.status is MemoryStatus.PROVISIONAL for belief in by_subject.values())


async def test_automatic_formation_uses_only_the_owning_principal_user_events() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    trusted = await user_event(factory, "I live in Seattle.")
    async with factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id="another-principal",
                payload={"content": "I own a Ferrari."},
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="assistant.message.completed",
                actor_type="model",
                payload={"content": "The user owns a private jet."},
            )
        )

    result = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )

    assert [(belief.subject, belief.source_event_ids) for belief in result.beliefs] == [
        ("home location", [trusted])
    ]
    assert result.beliefs[0].statement == "User lives in Seattle."


async def test_terminal_run_is_flagged_then_consolidated_after_idle(tmp_path: Path) -> None:
    clock = FixedClock(NOW)
    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")
    script = FakeModelScript(turns=[ScriptedTurn(text="Thanks for telling me.")])

    async with build(settings=settings, storage="memory", script=script, clock=clock) as app:
        run_id = await app.runs.submit("I have an Apple Watch and a BMW X3.")
        run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            before_idle = await uow.events.list_after(run.session_id, 0, app.principal)

        assert [
            event.event_type
            for event in before_idle
            if event.event_type == "memory.formation.requested"
        ] == ["memory.formation.requested"]
        assert await app.memory.list_memories() == []

        maintenance = cast(MaintenanceWorker, app.maintenance_factory())
        clock.advance(timedelta(seconds=29))
        await maintenance.run_once()
        assert await app.memory.list_memories() == []

        clock.advance(timedelta(seconds=1))
        await maintenance.run_once()
        memories = await app.memory.list_memories()

    assert {memory.subject for memory in memories} == {"Apple Watch", "BMW X3"}


async def test_automatic_formation_caps_candidates_and_rejects_secrets() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "I have an API key: secret=do-not-store.")
    owned = " and ".join(f"a Device-{index}" for index in range(1, 30))
    await user_event(factory, f"I have {owned}.")

    result = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )

    assert len(result.beliefs) == 11
    assert all("do-not-store" not in belief.statement for belief in result.beliefs)
    assert result.run.candidates_proposed == 30
    assert result.run.rejected == 19


async def test_service_enforces_candidate_cap_against_an_overproducing_extractor() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "I own several durable devices.")
    candidates = [
        MemoryCandidate(
            belief_type=BeliefType.USER_MODEL_ATTR,
            subject=f"Device {index}",
            statement=f"User owns Device {index}.",
            source_event_ids=[source],
            model_confidence=0.75,
            proposed_scope="project-a",
            proposed_portability=Portability.PORTABLE,
            sensitivity_guess=Sensitivity.INTERNAL,
        )
        for index in range(13)
    ]
    service = GovernedMemoryService(
        factory,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(4_000, 5_000)),
        principal(),
        extractor=_ScriptedCandidateExtractor(candidates),
    )

    result = await service.run(
        trigger="session_idle",
        scope="project-a",
        session_id=SESSION_ID,
    )

    assert len(result.beliefs) == 12
    assert result.run.candidates_proposed == 13
    assert result.run.rejected == 1


async def test_service_rejects_extractor_scope_escalation() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "I own an Apple Watch.")
    async with factory() as uow:
        foreign_source = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id="another-principal",
                payload={"content": "I own a Ferrari."},
            )
        )
    candidates = [
        MemoryCandidate(
            belief_type=BeliefType.USER_MODEL_ATTR,
            subject="Apple Watch",
            statement="User owns an Apple Watch.",
            source_event_ids=[source],
            model_confidence=0.75,
            proposed_scope="global",
            proposed_portability=Portability.PORTABLE,
            sensitivity_guess=Sensitivity.INTERNAL,
        ),
        MemoryCandidate(
            belief_type=BeliefType.USER_MODEL_ATTR,
            subject="Ferrari",
            statement="User owns a Ferrari.",
            source_event_ids=[foreign_source.sequence],
            model_confidence=0.75,
            proposed_scope="project-a",
            proposed_portability=Portability.PORTABLE,
            sensitivity_guess=Sensitivity.INTERNAL,
        ),
    ]
    service = GovernedMemoryService(
        factory,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(5_000, 6_000)),
        principal(),
        extractor=_ScriptedCandidateExtractor(candidates),
    )

    result = await service.run(
        trigger="session_idle",
        scope="project-a",
        session_id=SESSION_ID,
    )

    assert result.beliefs == []
    assert result.run.rejected == 2


async def test_inferred_candidates_cannot_claim_explicit_user_confidence() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "I own an Apple Watch.")
    candidate = MemoryCandidate(
        belief_type=BeliefType.USER_MODEL_ATTR,
        subject="Apple Watch",
        statement="User owns an Apple Watch.",
        source_event_ids=[source],
        model_confidence=1.0,
        proposed_scope="project-a",
        proposed_portability=Portability.PORTABLE,
        sensitivity_guess=Sensitivity.INTERNAL,
    )
    service = GovernedMemoryService(
        factory,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(5_500, 6_000)),
        principal(),
        extractor=_ScriptedCandidateExtractor([candidate]),
    )

    result = await service.run(
        trigger="session_idle",
        scope="project-a",
        session_id=SESSION_ID,
    )

    assert result.beliefs[0].confidence == pytest.approx(0.55)
    assert result.beliefs[0].authority is MemoryAuthority.INFERRED
    assert result.beliefs[0].status is MemoryStatus.PROVISIONAL


async def test_consolidation_audit_measures_extraction_and_commit_duration() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "I own an Apple Watch.")
    service = GovernedMemoryService(
        factory,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(6_000, 7_000)),
        principal(),
        extractor=_AdvancingCandidateExtractor(clock),
    )

    result = await service.run(
        trigger="session_idle",
        scope="project-a",
        session_id=SESSION_ID,
    )

    assert result.run.started_at == NOW
    assert result.run.finished_at == NOW + timedelta(seconds=5)


async def test_negated_possessive_emits_only_a_retraction_candidate() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "I no longer use my Apple Watch.")

    candidates = await DeterministicCandidateExtractor().extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert [
        (candidate.subject, candidate.polarity, candidate.source_event_ids)
        for candidate in candidates
    ] == [("Apple Watch", Polarity.RETRACT, [source])]


async def test_unrelated_unclassified_preferences_do_not_share_a_conflict_key() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "I prefer jasmine tea. I prefer morning meetings.")

    await service.run(trigger="session_idle", scope="general", session_id=SESSION_ID)
    current = await service.list_memories()

    assert {record.statement for record in current} == {
        "User prefers jasmine tea.",
        "User prefers morning meetings.",
    }


async def test_old_pending_in_memory_session_is_not_starved_by_newer_sessions() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    async with factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="memory.formation.requested",
                actor_type="runtime",
                derivation_key="old-pending-session",
            )
        )
        for index in range(10):
            session_id = UUID(int=7_000 + index)
            await uow.sessions.create(
                Session(
                    id=session_id,
                    tenant_id=TENANT,
                    principal_id=principal().principal_id,
                    agent_id=AGENT_ID,
                    agent_version="1.0.0",
                    status=SessionStatus.ACTIVE,
                    created_at=NOW + timedelta(minutes=index + 2),
                    updated_at=NOW + timedelta(minutes=index + 2),
                )
            )

    async with factory() as uow:
        assert (
            await uow.maintenance.pending_memory_sessions(
                principal(),
                idle_before=NOW + timedelta(minutes=1),
                limit=0,
            )
            == []
        )
        pending = await uow.maintenance.pending_memory_sessions(
            principal(),
            idle_before=NOW + timedelta(minutes=1),
            limit=1,
        )

    assert pending == [SESSION_ID]


async def test_same_terminal_run_enqueues_only_one_formation_flag(tmp_path: Path) -> None:
    clock = FixedClock(NOW)
    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")
    script = FakeModelScript(turns=[ScriptedTurn(text="Noted.")])

    async with build(settings=settings, storage="memory", script=script, clock=clock) as app:
        run_id = await app.runs.submit("I live in Seattle.")
        run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal)

    assert sum(event.event_type == "memory.formation.requested" for event in events) == 1
    assert [
        event.derivation_key for event in events if event.event_type == "memory.formation.requested"
    ] == [f"memory.formation.requested:{run_id}"]


async def test_memory_events_are_not_reprocessed_as_user_sources() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "I live in Seattle.")
    first = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )
    assert len(first.beliefs) == 1

    second = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )

    assert second.beliefs == []
    assert [event.event_type for event in await session_events(factory)].count("memory.formed") == 1


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "My wife is Morgan.",
            {("wife", "User's wife is Morgan.", BeliefType.RELATIONSHIP)},
        ),
        (
            "I work at Acme Labs.",
            {("employment", "User works at Acme Labs.", BeliefType.USER_MODEL_ATTR)},
        ),
        (
            "We decided to deploy on Fridays.",
            {("project decision", "The team decided to deploy on Fridays.", BeliefType.FACT)},
        ),
        (
            "We shipped version 2.",
            {("task outcome", "The team shipped version 2.", BeliefType.FACT)},
        ),
        (
            "We discussed both my Apple Watch and my BMW X3.",
            {
                ("Apple Watch", "User has an Apple Watch.", BeliefType.USER_MODEL_ATTR),
                ("BMW X3", "User has a BMW X3.", BeliefType.USER_MODEL_ATTR),
            },
        ),
    ],
)
async def test_automatic_formation_covers_documented_salient_span_types(
    message: str,
    expected: set[tuple[str, str, BeliefType]],
) -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, message)

    result = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )

    assert {
        (belief.subject, belief.statement, belief.belief_type) for belief in result.beliefs
    } == expected


async def test_independent_clauses_and_text_parts_form_independent_candidates() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "I prefer jasmine tea and we decided to deploy on Fridays.")
    async with factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={
                    "content": [
                        {"kind": "text", "text": "I own an Apple Watch."},
                        {"kind": "text", "text": "I live in Seattle."},
                    ]
                },
            )
        )

    candidates = await DeterministicCandidateExtractor().extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert {candidate.subject for candidate in candidates} == {
        "tea preference",
        "project decision",
        "Apple Watch",
        "home location",
    }


async def test_ordinary_retraction_supersedes_the_owned_entity_memory() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(
        factory,
        "I have an Apple Watch and a BMW X3. I prefer concise answers. I prefer dark mode.",
    )
    first = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )
    assert {belief.statement for belief in first.beliefs} == {
        "User has an Apple Watch.",
        "User has a BMW X3.",
        "User prefers concise answers.",
        "User prefers dark mode.",
    }

    await user_event(factory, "I no longer have a BMW X3.")
    second = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )
    records = await service.list_memories(include_inactive=True)

    assert second.beliefs[0].statement == "User no longer has a BMW X3."
    assert second.beliefs[0].polarity.value == "retract"
    assert second.run.committed == 1
    assert second.run.superseded == 1
    assert {record.status for record in records} == {
        MemoryStatus.PROVISIONAL,
        MemoryStatus.SUPERSEDED,
    }
    assert any(
        record.subject == "Apple Watch" and record.status is MemoryStatus.PROVISIONAL
        for record in records
    )
    assert {record.subject for record in records if record.status is MemoryStatus.PROVISIONAL} == {
        "Apple Watch",
        "BMW X3",
        "answer style",
        "interface theme",
    }


async def test_independent_preference_topics_coexist_while_a_correction_supersedes() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "I prefer concise answers. I prefer dark mode.")
    first = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )
    assert {belief.subject for belief in first.beliefs} == {"answer style", "interface theme"}

    await user_event(factory, "I prefer detailed answers.")
    correction = await service.run(
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
    )
    records = await service.list_memories(include_inactive=True)
    current = {record.subject: record.statement for record in records if record.valid_to is None}

    assert current == {
        "answer style": "User prefers detailed answers.",
        "interface theme": "User prefers dark mode.",
    }
    assert correction.run.committed == 1
    assert correction.run.superseded == 1
