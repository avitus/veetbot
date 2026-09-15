"""A terminal delta checkpoint must not starve independent maintenance work."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.support import NOW, RUN_ID, memory_uow_factory


async def test_terminal_delta_checkpoint_does_not_block_memory_formation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock, factory = await memory_uow_factory()
    healthy_id = UUID("00000000-0000-0000-0000-000000000031")
    checkpoint = RunCheckpoint(run_id=RUN_ID, version=1, status=RunStatus.RUNNING, created_at=NOW)
    async with factory() as uow:
        # Production had a failed run with six checkpoints ending in a delta.
        for version in range(1, 7):
            await uow.checkpoints.write(
                RUN_ID, checkpoint.model_copy(update={"version": version}), full=version == 1
            )
        healthy = checkpoint.model_copy(update={"run_id": healthy_id})
        await uow.checkpoints.write(healthy_id, healthy, full=True)
        healthy = healthy.model_copy(update={"version": 2, "status": RunStatus.COMPLETED})
        await uow.checkpoints.write(healthy_id, healthy, full=True)

        async def selected_runs(limit: int) -> list[tuple[UUID, bool]]:
            return [(RUN_ID, True), (healthy_id, True)][:limit]

        monkeypatch.setattr(uow.maintenance, "checkpoint_runs", selected_runs)

    memory_sweeps = 0

    async def consolidate() -> int:
        nonlocal memory_sweeps
        assert not factory.is_open()
        memory_sweeps += 1
        return 0

    worker = MaintenanceWorker(
        uow_factory=factory, clock=clock, sweep_memory_consolidation=consolidate
    )
    await worker.run_once()

    assert memory_sweeps == 1
    async with factory() as uow:
        assert await uow.checkpoints.latest(RUN_ID) == checkpoint.model_copy(update={"version": 6})
        with pytest.raises(ConflictError, match="final full snapshot"):
            await uow.checkpoints.prune(RUN_ID, terminal=True)
        assert await uow.checkpoints.prune(healthy_id, terminal=True) == 0
        assert await uow.checkpoints.latest(healthy_id) == healthy
    assert "checkpoint prune failed" in caplog.text
    assert str(RUN_ID) in caplog.text
    assert "ConflictError" in caplog.text
    assert "terminal checkpoint retention" not in caplog.text


async def test_checkpoint_prune_cancellation_stops_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, factory = await memory_uow_factory()
    async with factory() as uow:

        async def selected_runs(limit: int) -> list[tuple[UUID, bool]]:
            return [(RUN_ID, True)][:limit]

        async def cancelled_prune(run_id: UUID, *, terminal: bool) -> int:
            raise asyncio.CancelledError

        monkeypatch.setattr(uow.maintenance, "checkpoint_runs", selected_runs)
        monkeypatch.setattr(uow.checkpoints, "prune", cancelled_prune)

    async def consolidate() -> int:
        pytest.fail("shutdown must not start memory formation")

    worker = MaintenanceWorker(
        uow_factory=factory, clock=clock, sweep_memory_consolidation=consolidate
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()
