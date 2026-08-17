"""PostgreSQL coverage for the Milestone 10 idle-consolidation lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.persistence.repositories import PostgresMaintenanceRepository
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import MemoryCandidate
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.memory.formation import DeterministicCandidateExtractor, GovernedMemoryService
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings


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
        await self._ready.wait()
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
        assert sum(event.event_type == "memory.formation.requested" for event in events) == 1

        clock.advance(timedelta(seconds=30))
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
            await claims_ready.wait()
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
