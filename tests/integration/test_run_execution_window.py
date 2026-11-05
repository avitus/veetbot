"""A queued run's execution window starts at its first claim (ADR-0295)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import update

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.sqlalchemy_models import RunRow
from agent_core.bootstrap import Composition, build
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings

WINDOW = timedelta(seconds=120)
OUTER = NOW + timedelta(seconds=420)


@asynccontextmanager
async def windowed_run(clock: FixedClock) -> AsyncIterator[tuple[Composition, UUID]]:
    """Submit a run admitted with a 120 s window inside a 420 s outer deadline."""
    async with build(settings=database_settings(), storage="postgres", clock=clock) as composition:
        run_id = await composition.runs.submit("refresh the mail")
        run = await composition.runs.get(run_id)
        limits = run.limits.model_copy(update={"deadline_at": OUTER, "execution_seconds": 120})
        engine = create_engine(composition.settings.database_url)
        try:
            async with create_session_factory(engine)() as session:
                await session.execute(
                    update(RunRow)
                    .where(RunRow.id == run_id)
                    .values(limits=limits.model_dump(mode="json"), deadline_at=OUTER)
                )
                await session.commit()
        finally:
            await engine.dispose()
        yield composition, run_id


async def test_time_spent_queued_does_not_spend_the_execution_window() -> None:
    clock = FixedClock(NOW)
    async with windowed_run(clock) as (composition, run_id):
        # The production failure: 209 s behind another run against a 120 s limit.
        clock.advance(timedelta(seconds=209))
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            claimed = await uow.queue.claim("worker", [0])
        assert claimed is not None
        assert claimed.run.id == run_id
        assert claimed.run.deadline_at == clock.now() + WINDOW
        assert claimed.run.limits.deadline_at == clock.now() + WINDOW
        stored = await composition.runs.get(run_id)
        assert stored.deadline_at == clock.now() + WINDOW
        assert stored.limits.deadline_at == clock.now() + WINDOW


async def test_a_reclaimed_run_keeps_the_window_of_its_first_claim() -> None:
    clock = FixedClock(NOW)
    async with windowed_run(clock) as (composition, run_id):
        clock.advance(timedelta(seconds=10))
        started = clock.now()
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            assert await uow.queue.claim("crashed-worker", [0]) is not None
        clock.advance(timedelta(seconds=31))
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            assert await uow.queue.reclaim_expired(10) == 1
        clock.advance(timedelta(seconds=1))
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            again = await uow.queue.claim("worker", [0])
        assert again is not None
        assert again.run.id == run_id
        assert again.run.deadline_at == started + WINDOW


@pytest.mark.parametrize(
    "waited",
    [
        pytest.param(350, id="a late claim gets what remains"),
        pytest.param(421, id="a claim past the outer deadline stays past it"),
    ],
)
async def test_the_window_never_extends_past_the_outer_deadline(waited: int) -> None:
    clock = FixedClock(NOW)
    async with windowed_run(clock) as (composition, _run_id):
        clock.advance(timedelta(seconds=waited))
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            claimed = await uow.queue.claim("worker", [0])
        assert claimed is not None
        assert claimed.run.deadline_at == OUTER
        assert claimed.run.limits.deadline_at == OUTER


async def test_a_run_without_a_window_keeps_the_deadline_set_at_creation() -> None:
    clock = FixedClock(NOW)
    async with build(settings=database_settings(), storage="postgres", clock=clock) as composition:
        run_id = await composition.runs.submit("an ordinary turn")
        created = await composition.runs.get(run_id)
        clock.advance(timedelta(seconds=30))
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            claimed = await uow.queue.claim("worker", [0])
        assert claimed is not None
        assert claimed.run.deadline_at == created.deadline_at
        assert claimed.run.limits == created.limits
