"""M32 inventory and background isolation leave ordinary maintenance responsive."""

import asyncio
from typing import cast

from agent_core.domain.reconsolidation import ReconsolidationJob
from agent_core.memory.reconsolidation import ReconsolidationInventoryPass
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.support import memory_uow_factory, principal


async def test_inventory_queues_bounded_groups_and_preserves_originals() -> None:
    clock, factory = await memory_uow_factory()
    originals = [
        memory(belief_id=1000 + i).model_copy(update={"store_position": i + 1}) for i in range(40)
    ]
    async with factory() as uow:
        for record in originals:
            await uow.memories.upsert_belief(record)
    admitted = False
    service = ReconsolidationInventoryPass(
        factory, clock, principal(), worker_id="test", admitted=lambda: admitted
    )
    assert await service.run_once() == 0
    admitted = True
    assert 1 <= await service.run_once() <= 4, "eligible groups must be durably queued"
    async with factory() as uow:
        for record in originals:
            assert await uow.memories.get(record.id, principal()) == record
        lease = await uow.reconsolidation.claim_due(principal(), clock.now(), "next")
        assert lease is not None and lease.full_cursor == lease.full_bound
        group = await uow.reconsolidation.claim_group(principal(), lease.lease_token, clock.now())
        assert group is not None and group.attempts == 1


async def test_background_slice_does_not_block_sweeps_and_stops_cleanly() -> None:
    clock, factory = await memory_uow_factory()
    started, cancelled = asyncio.Event(), asyncio.Event()
    sweeps = 0
    calls = 0

    async def background() -> int:
        nonlocal calls
        calls += 1
        assert not factory.is_open()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return 0

    async def ordinary() -> int:
        nonlocal sweeps
        sweeps += 1
        return 0

    worker = MaintenanceWorker(
        uow_factory=factory,
        clock=clock,
        sweep_memory_reconsolidation=background,
        sweep_memory=ordinary,
    )
    await worker.run_once()
    await asyncio.sleep(0)
    assert started.is_set(), "maintenance should dispatch the bounded background slice"
    await worker.run_once()
    assert sweeps == 2 and calls == 1
    worker.stop()
    await worker.run_forever()
    assert cancelled.is_set()
    await worker.run_once()
    assert calls == 1


async def test_inventory_processes_claimed_work_before_yielding_lease() -> None:
    clock, factory = await memory_uow_factory()
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        await uow.memories.upsert_belief(
            memory(belief_id=502).model_copy(update={"store_position": 2})
        )
    processed = []

    class ProcessingPass(ReconsolidationInventoryPass):
        async def _process(self, lease: ReconsolidationJob) -> int:
            async with factory() as uow:
                group = await uow.reconsolidation.claim_group(
                    principal(), lease.lease_token, clock.now()
                )
                assert group is not None
                processed.append(group.id)
                await uow.reconsolidation.finish_group(
                    principal(), lease.lease_token, group.id, "no_change", clock.now()
                )
            return 1

    service = ProcessingPass(
        factory, clock, principal(), worker_id="processor", admitted=lambda: True
    )
    assert await service.run_once() == 1
    assert len(processed) == 1, "queued work must run on the same slice before releasing the lease"


async def test_complete_slice_executes_two_calls_and_finishes_groups() -> None:
    from agent_core.adapters.determinism import RandomIdFactory
    from agent_core.memory.reconsolidation_worker import ReconsolidationPass
    from tests.contract.reconsolidation_admission_cases import model, policy
    from tests.contract.reconsolidation_execution_cases import ExecutionProvider, TrackedFactory
    from tests.contract.reconsolidation_source_cases import seed_sources

    clock, factory = await memory_uow_factory()
    tracked = TrackedFactory(cast(Factory, factory))
    async with factory() as uow:
        await seed_sources(cast(Stores, uow))
    provider = ExecutionProvider(tracked)
    service = ReconsolidationPass(
        cast(UnitOfWorkFactory, tracked),
        clock,
        principal(),
        worker_id="complete",
        admitted=lambda: True,
        ids=RandomIdFactory(),
        model=model(),
        provider=provider,
        egress_policy=policy,
    )
    assert await service.run_once() == 1
    assert len(provider.requests) == 2
    assert await service.run_once() == 0
    assert len(provider.requests) == 2
    async with factory() as uow:
        assert await uow.reconsolidation.claim_due(principal(), clock.now(), "other") is None


async def test_processing_startup_refuses_missing_activation_evidence() -> None:
    from dataclasses import replace

    import pytest

    from agent_core.bootstrap import build
    from agent_core.config import ConfigurationError
    from tests.integration.m2_support import memory_settings

    with pytest.raises(ConfigurationError, match="reconsolidation"):
        async with build(
            settings=replace(memory_settings(), memory_reconsolidation_enabled=True),
            storage="memory",
        ):
            pass
