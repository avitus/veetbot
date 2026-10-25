"""The maintenance role commits Cockpit widgets and imports the branch (docs/plan/cockpit.md)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.support import memory_uow_factory


async def test_the_outbox_runs_every_round_and_the_importer_on_its_poll_interval() -> None:
    clock, uow_factory = await memory_uow_factory()
    calls: list[str] = []

    async def outbox() -> int:
        calls.append("outbox")
        return 0

    async def importer() -> int:
        calls.append("import")
        return 0

    worker = MaintenanceWorker(
        uow_factory=uow_factory,
        clock=clock,
        sweep_cockpit_outbox=outbox,
        sweep_cockpit_import=importer,
        cockpit_import_interval_seconds=60,
    )
    await worker.run_once()
    clock.advance(timedelta(seconds=30))
    await worker.run_once()
    clock.advance(timedelta(seconds=30))
    await worker.run_once()

    assert calls == ["outbox", "import", "outbox", "outbox", "import"]


async def test_a_failing_cockpit_sweep_leaves_the_round_standing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, uow_factory = await memory_uow_factory()
    later: list[str] = []

    async def failing() -> int:
        raise RuntimeError("GitHub down")

    async def email_cache() -> int:
        later.append("email cache")
        return 0

    worker = MaintenanceWorker(
        uow_factory=uow_factory,
        clock=clock,
        sweep_email_cache=email_cache,
        sweep_cockpit_outbox=failing,
        sweep_cockpit_import=failing,
    )
    await worker.run_once()

    assert later == ["email cache"]
    assert "Cockpit commit outbox sweep failed" in caplog.text
    assert "Cockpit branch import sweep failed" in caplog.text


def test_the_import_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="Cockpit import interval"):
        MaintenanceWorker(
            uow_factory=None,  # type: ignore[arg-type]
            clock=None,  # type: ignore[arg-type]
            cockpit_import_interval_seconds=0,
        )


async def test_event_retention_runs_on_the_import_timer() -> None:
    clock, uow_factory = await memory_uow_factory()
    calls: list[str] = []

    async def events() -> int:
        calls.append("events")
        return 0

    worker = MaintenanceWorker(
        uow_factory=uow_factory,
        clock=clock,
        sweep_cockpit_events=events,
        cockpit_import_interval_seconds=60,
    )
    await worker.run_once()
    clock.advance(timedelta(seconds=30))
    await worker.run_once()
    clock.advance(timedelta(seconds=30))
    await worker.run_once()

    assert calls == ["events", "events"]


async def test_the_feed_projector_runs_on_its_own_interval_and_history_on_the_import_timer() -> (
    None
):
    clock, uow_factory = await memory_uow_factory()
    calls: list[str] = []

    async def feeds() -> int:
        calls.append("feeds")
        return 0

    async def history() -> int:
        calls.append("history")
        return 0

    worker = MaintenanceWorker(
        uow_factory=uow_factory,
        clock=clock,
        sweep_cockpit_feeds=feeds,
        sweep_cockpit_feed_history=history,
        cockpit_feed_interval_seconds=30,
        cockpit_import_interval_seconds=60,
    )
    for _ in range(5):
        await worker.run_once()
        clock.advance(timedelta(seconds=15))

    assert calls == ["feeds", "history", "feeds", "feeds", "history"]


def test_the_feed_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="Cockpit feed interval"):
        MaintenanceWorker(
            uow_factory=None,  # type: ignore[arg-type]
            clock=None,  # type: ignore[arg-type]
            cockpit_feed_interval_seconds=0,
        )
