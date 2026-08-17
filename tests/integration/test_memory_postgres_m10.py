"""PostgreSQL coverage for the Milestone 10 idle-consolidation lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.persistence.repositories import PostgresMaintenanceRepository
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import BeliefType, MemoryCandidate
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.memory.formation import DeterministicCandidateExtractor, GovernedMemoryService
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings

_BARRIER_TIMEOUT = 10.0


class _BarrierExtractor:
    name = "barrier-deterministic-v2"

    def __init__(self) -> None:
        self._arrivals = 0
        self._ready = asyncio.Event()
        self._delegate = DeterministicCandidateExtractor()

    async def extract(
        self,
        events: list[EventEnvelope],
        *,
        principal: Principal,
        scope: str,
    ) -> list[MemoryCandidate]:
        self._arrivals += 1
        if self._arrivals == 2:
            self._ready.set()
        await asyncio.wait_for(self._ready.wait(), _BARRIER_TIMEOUT)
        return await self._delegate.extract(events, principal=principal, scope=scope)


async def test_postgres_terminal_flag_drives_idle_memory_consolidation(tmp_path: Path) -> None:
    clock = FixedClock(NOW)
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    script = FakeModelScript(turns=[ScriptedTurn(text="Thanks for telling me.")])

    async with build(settings=settings, storage="postgres", script=script, clock=clock) as app:
        run_id = await app.runs.submit("I have an Apple Watch and a BMW X3.")
        worker = DurableWorker(
            uow_factory=app.uow_factory,
            executor=app.executor,
            clock=clock,
            worker_id="memory-m10-worker",
        )
        assert await worker.run_once()
        run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal)
            formation_event = next(
                event for event in events if event.event_type == "memory.formation.requested"
            )
            not_before = datetime.fromisoformat(cast(str, formation_event.payload["not_before"]))
            assert (
                await uow.maintenance.pending_memory_sessions(
                    app.principal,
                    idle_before=formation_event.created_at - timedelta(microseconds=1),
                    ready_at=not_before,
                    limit=10,
                )
                == []
            )
            assert (
                await uow.maintenance.pending_memory_sessions(
                    app.principal,
                    idle_before=formation_event.created_at,
                    ready_at=not_before - timedelta(microseconds=1),
                    limit=10,
                )
                == []
            )
            assert await uow.maintenance.pending_memory_sessions(
                app.principal,
                idle_before=formation_event.created_at,
                ready_at=not_before,
                limit=10,
            ) == [run.session_id]
        assert sum(event.event_type == "memory.formation.requested" for event in events) == 1

        clock.advance(not_before - clock.now())
        maintenance = cast(MaintenanceWorker, app.maintenance_factory())
        await maintenance.run_once()
        memories = await app.memory.list_memories()

    assert {memory.subject for memory in memories} == {"Apple Watch", "BMW X3"}
    assert all(memory.source_session_id == run.session_id for memory in memories)


async def test_postgres_concurrent_consolidators_form_each_candidate_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FixedClock(NOW)
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    script = FakeModelScript(turns=[ScriptedTurn(text="Thanks for telling me.")])

    async with build(settings=settings, storage="postgres", script=script, clock=clock) as app:
        run_id = await app.runs.submit("I have an Apple Watch.")
        worker = DurableWorker(
            uow_factory=app.uow_factory,
            executor=app.executor,
            clock=clock,
            worker_id="memory-race-run-worker",
        )
        assert await worker.run_once()
        run = await app.runs.get(run_id)
        extractor = _BarrierExtractor()
        original_acquire = PostgresMaintenanceRepository.acquire_memory_session
        claim_arrivals = 0
        claims_ready = asyncio.Event()

        async def synchronized_acquire(
            repository: PostgresMaintenanceRepository,
            principal: Principal,
            session_id: UUID,
        ) -> bool:
            nonlocal claim_arrivals
            claim_arrivals += 1
            if claim_arrivals == 2:
                claims_ready.set()
            await asyncio.wait_for(claims_ready.wait(), _BARRIER_TIMEOUT)
            return await original_acquire(repository, principal, session_id)

        monkeypatch.setattr(
            PostgresMaintenanceRepository,
            "acquire_memory_session",
            synchronized_acquire,
        )
        services = [
            GovernedMemoryService(
                app.uow_factory,
                clock,
                SequenceIdFactory(UUID(int=value) for value in range(offset, offset + 1_000)),
                app.principal,
                extractor=extractor,
            )
            for offset in (8_000, 9_000)
        ]

        results = await asyncio.gather(
            *(
                service.run(trigger="session_idle", scope="general", session_id=run.session_id)
                for service in services
            )
        )
        memories = await app.memory.list_memories(include_inactive=True)

    assert [memory.subject for memory in memories] == ["Apple Watch"]
    assert sum(result.run.committed for result in results) == 1


async def test_postgres_failed_stale_supersede_rolls_back_its_replacement(
    tmp_path: Path,
) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with build(settings=settings, storage="postgres") as app:
        session_id = await app.sessions.create()
        async with app.uow_factory() as uow:
            first_source = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": "I prefer concise answers."},
                )
            )
        current = await app.memory.remember(
            session_id=session_id,
            run_id=None,
            statement="User prefers concise answers.",
            subject="answer style",
            scope="general",
            belief_type=BeliefType.PREFERENCE,
            source_event_ids=[first_source.sequence],
        )
        async with app.uow_factory() as uow:
            second_source = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": "I prefer detailed answers."},
                )
            )
        replacement = await app.memory.remember(
            session_id=session_id,
            run_id=None,
            statement="User prefers detailed answers.",
            subject="answer style",
            scope="general",
            belief_type=BeliefType.PREFERENCE,
            source_event_ids=[second_source.sequence],
        )

        orphan_id = uuid4()
        async with app.uow_factory() as uow:
            stale = await uow.memories.get(current.id, app.principal)
            orphan = replacement.model_copy(
                update={
                    "id": orphan_id,
                    "statement": "User prefers medium-length answers.",
                    "formation_run_id": uuid4(),
                    "store_position": await uow.memories.next_position(),
                }
            )
            with pytest.raises(ConflictError, match="already inactive"):
                await uow.memories.supersede(stale, orphan)

        async with app.uow_factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.memories.get(orphan_id, app.principal)
