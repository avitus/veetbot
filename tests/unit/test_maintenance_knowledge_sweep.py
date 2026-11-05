"""ADR-0290: the knowledge vector backfill is bounded so a slow provider cannot hold the pass."""

from __future__ import annotations

import asyncio

import pytest

from agent_core.runtime import worker as worker_module
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.support import memory_uow_factory


async def test_a_slow_knowledge_vector_sweep_ends_and_the_round_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock, uow_factory = await memory_uow_factory()
    later: list[str] = []
    monkeypatch.setattr(worker_module, "KNOWLEDGE_VECTOR_SWEEP_SECONDS", 0.01)

    async def slow() -> int:
        await asyncio.sleep(0.3)
        return 64

    async def titles() -> int:
        later.append("titles")
        return 0

    worker = MaintenanceWorker(
        uow_factory=uow_factory,
        clock=clock,
        sweep_knowledge_vectors=slow,
        sweep_conversation_titles=titles,
    )
    await worker.run_once()

    assert later == ["titles"]
    assert "knowledge vector backfill sweep ended (TimeoutError)" in caplog.text
