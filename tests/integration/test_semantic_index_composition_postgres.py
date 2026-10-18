"""Admitted indexing cooperates with durable run leases and source restrictions."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_core.bootstrap import build
from agent_core.domain.memory import Sensitivity
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import RunStatus
from agent_core.memory.semantic_deployment import compose_indexing
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.memory_fixtures import memory
from tests.contract.semantic_indexing_cases import Provider
from tests.contract.support import RUN_ID, agent, principal, run, session
from tests.integration.m2_support import database_settings
from tests.unit.test_semantic_index_composition import configured_index


async def test_index_admission_lease_deferral_and_withdrawal(tmp_path: Path) -> None:
    kwargs, _, _, manifest, _ = await configured_index(tmp_path)
    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=kwargs["clock"],
    ) as app:
        provider = Provider(app.uow_factory)
        kwargs["factory"] = app.uow_factory
        kwargs["provider_factory"] = lambda _: provider
        index = await compose_indexing(**kwargs)
        assert index is not None
        async with app.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.sessions.create(session())
            await uow.runs.create(
                run(status=RunStatus.RUNNING).model_copy(
                    update={
                        "lease_owner": "foreground",
                        "lease_epoch": 1,
                        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=5),
                    }
                )
            )
            await uow.memories.upsert_belief(memory(statement="User prefers morning walks"))
            await uow.memories.upsert_belief(
                memory(belief_id=777, statement="User prefers quiet evenings").model_copy(
                    update={"sensitivity": Sensitivity.SENSITIVE, "store_position": 2}
                )
            )

        async def sweep() -> int:
            return (await index.index.run_slice()).indexed

        worker = MaintenanceWorker(
            uow_factory=app.uow_factory, clock=kwargs["clock"], sweep_memory_semantic_index=sweep
        )
        await worker.run_once()
        await asyncio.sleep(0)
        assert worker._semantic_index_task is None and provider.calls == []
        async with app.uow_factory() as uow:
            assert uow.queue is not None
            await uow.queue.release(
                WorkerLease(run_id=RUN_ID, worker_id="foreground", lease_epoch=1), RunStatus.RUNNING
            )
        await worker.run_once()
        assert worker._semantic_index_task is not None
        await worker._semantic_index_task
        assert len(provider.calls) == 1 and len(provider.calls[0].texts) == 1
        assert "morning walks" in provider.calls[0].texts[0]
        manifest.unlink()
        await worker.run_once()
        await worker._semantic_index_task
        assert len(provider.calls) == 1
        worker.stop()
        await worker.run_forever()
