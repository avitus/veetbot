"""PostgreSQL coverage for the Milestone 10 idle-consolidation lifecycle."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings


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
