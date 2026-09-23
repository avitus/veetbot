"""Crash allowance is independent of clarification, approval, and child resumes."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.sqlalchemy_models import RunRow
from agent_core.bootstrap import build
from agent_core.domain.runs import FailureReason, RunStatus
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("continuation", ["input", "approval", "child"])
async def test_continuations_preserve_crash_allowance_and_fencing(continuation: str) -> None:
    waiting = (
        RunStatus.WAITING_FOR_USER if continuation == "input" else RunStatus.WAITING_FOR_APPROVAL
    )
    clock = FixedClock(NOW)
    async with build(settings=database_settings(), storage="postgres", clock=clock) as composition:
        run_id = await composition.runs.submit("continue safely")
        epoch = 0
        for expirations in range(1, 4):
            # Several successful suspensions between actual worker failures must
            # neither spend nor replenish the crash allowance.
            for _ in range(4):
                async with composition.uow_factory() as uow:
                    assert uow.queue is not None
                    claimed = await uow.queue.claim("worker", [0])
                    assert claimed is not None
                    assert claimed.run.lease_expirations == expirations - 1
                    assert claimed.lease.lease_epoch > epoch
                    epoch = claimed.lease.lease_epoch
                    suspended = await uow.runs.transition(
                        run_id, RunStatus.RUNNING, waiting, lease=claimed.lease
                    )
                    await uow.queue.release(claimed.lease, waiting)
                    if waiting is RunStatus.WAITING_FOR_USER:
                        await composition.executor.requeue_after_input(uow, suspended)
                    elif continuation == "approval":
                        await composition.executor.requeue_after_approval(uow, suspended)
                    else:
                        await composition.executor.requeue_after_child(uow, suspended)
            async with composition.uow_factory() as uow:
                assert uow.queue is not None
                crashed = await uow.queue.claim("crashed-worker", [0])
                assert crashed is not None
            clock.advance(timedelta(seconds=31))
            async with composition.uow_factory() as uow:
                assert uow.queue is not None
                assert await uow.queue.reclaim_expired(10) == 1
                assert await uow.queue.heartbeat(crashed.lease) == (False, False)
            run = await composition.runs.get(run_id)
            assert run.lease_expirations == expirations
            if expirations < 3:
                assert run.status is RunStatus.QUEUED
                assert run.scheduled_for == clock.now() + timedelta(seconds=2 ** (expirations - 1))
                async with composition.uow_factory() as uow:
                    assert uow.queue is not None
                    assert await uow.queue.claim("early-worker", [0]) is None
                clock.advance(timedelta(seconds=2 ** (expirations - 1)))
            else:
                assert run.status is RunStatus.FAILED
                assert run.failure is not None
                assert run.failure.reason is FailureReason.MAX_ATTEMPTS_EXCEEDED
                assert run.failure.attempt_number == 3
        assert run.attempts == 15
        events = await composition.runs.events(run_id)
        assert sum(event.event_type == "run.failed" for event in events) == 1
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            assert await uow.queue.reclaim_expired(10) == 0
            assert await uow.queue.claim("late-worker", [0]) is None


async def test_exhausted_queued_run_fails_once_instead_of_remaining_unclaimable() -> None:
    clock = FixedClock(NOW)
    settings = database_settings()
    async with build(settings=settings, storage="postgres", clock=clock) as composition:
        run_id = await composition.runs.submit("exhausted recovery")
        engine = create_engine(settings.database_url)
        try:
            async with create_session_factory(engine)() as session:
                await session.execute(
                    update(RunRow).where(RunRow.id == run_id).values(lease_expirations=3)
                )
                await session.commit()
        finally:
            await engine.dispose()
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            assert await uow.queue.claim("worker", [0]) is None
            assert await uow.queue.reclaim_expired(1) == 1
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            assert await uow.queue.reclaim_expired(1) == 0
        run = await composition.runs.get(run_id)
        assert run.status is RunStatus.FAILED
        assert run.lease_expirations == 3
        assert run.lease_owner is None
        assert run.failure is not None
        assert run.failure.reason is FailureReason.MAX_ATTEMPTS_EXCEEDED
        failed = [e for e in await composition.runs.events(run_id) if e.event_type == "run.failed"]
        assert len(failed) == 1
        assert "reclaimed_epoch" not in failed[0].payload
