"""ADR-0155: the maintenance pass answers title requests on every round."""

from __future__ import annotations

import pytest

from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.support import memory_uow_factory


async def test_the_title_sweep_runs_every_round() -> None:
    clock, uow_factory = await memory_uow_factory()
    rounds: list[int] = []

    async def sweep() -> int:
        rounds.append(len(rounds))
        return 0

    worker = MaintenanceWorker(
        uow_factory=uow_factory, clock=clock, sweep_conversation_titles=sweep
    )
    await worker.run_once()
    await worker.run_once()

    assert rounds == [0, 1]


async def test_a_failing_title_sweep_leaves_the_round_standing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, uow_factory = await memory_uow_factory()
    later: list[str] = []

    async def failing() -> int:
        raise RuntimeError("title model down")

    async def email_cache() -> int:
        later.append("email cache")
        return 0

    worker = MaintenanceWorker(
        uow_factory=uow_factory,
        clock=clock,
        sweep_email_cache=email_cache,
        sweep_conversation_titles=failing,
    )
    await worker.run_once()

    # A sweep that runs after the title pass in the same round still ran.
    assert later == ["email cache"]
    assert "conversation title sweep failed" in caplog.text
