"""The schedule and notification loops outlive transient failures and stay bounded.

scheduling.md and notifications-and-devices.md give each lean role a bounded
fallback poll: a failed scan is logged and retried after the wait, a failed
wakeup degrades to the poll, and nothing waits longer than the poll interval.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.notification_worker import NotificationWorker
from agent_core.domain.schedules import ScheduleOccurrence
from agent_core.ports.persistence import ScheduleUnitOfWorkFactory
from agent_core.scheduling.worker import ScheduleWorker

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class _Schedules:
    def __init__(self, *, due: list[UUID], next_fire_at: datetime | None) -> None:
        self.due_ids = due
        self.next = next_fire_at
        self.failures = 0

    async def due(self, _now: datetime, batch: int) -> list[UUID]:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transient schedule read failure")
        return self.due_ids[:batch]

    async def next_fire_at(self) -> datetime | None:
        return self.next


class _UnitOfWork:
    def __init__(self, schedules: _Schedules) -> None:
        self.schedules = schedules

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def _factory(schedules: _Schedules) -> ScheduleUnitOfWorkFactory:
    def open_unit() -> _UnitOfWork:
        return _UnitOfWork(schedules)

    return cast(ScheduleUnitOfWorkFactory, open_unit)


async def _nothing(_schedule_id: UUID) -> ScheduleOccurrence | None:
    return None


def _schedule_worker(schedules: _Schedules, clock: FixedClock, **options: Any) -> ScheduleWorker:
    return ScheduleWorker(
        uow_factory=_factory(schedules),
        materialize=options.pop("materialize", _nothing),
        clock=clock,
        scan_batch=options.pop("scan_batch", 10),
        fallback_poll_seconds=options.pop("fallback_poll_seconds", 30),
        admission_backoff_seconds=options.pop("admission_backoff_seconds", 5),
        **options,
    )


@pytest.mark.parametrize(
    "knob",
    [
        {"scan_batch": 0},
        {"fallback_poll_seconds": 0},
        {"admission_backoff_seconds": -1},
    ],
)
def test_schedule_worker_refuses_unbounded_knobs(knob: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _schedule_worker(_Schedules(due=[], next_fire_at=None), FixedClock(NOW), **knob)


def test_notification_worker_refuses_an_unbounded_poll() -> None:
    async def dispatch() -> int:
        return 0

    with pytest.raises(ValueError, match="must be positive"):
        NotificationWorker(dispatch_once=dispatch, clock=FixedClock(NOW), fallback_poll_seconds=0)


@pytest.mark.parametrize(
    ("next_fire_at", "wait"),
    [(None, 30), (NOW + timedelta(hours=6), 30), (NOW + timedelta(seconds=12), 12)],
    ids=["nothing-scheduled", "far-future", "soon"],
)
async def test_the_schedule_wait_never_exceeds_the_fallback_poll(
    next_fire_at: datetime | None, wait: float
) -> None:
    worker = _schedule_worker(_Schedules(due=[], next_fire_at=next_fire_at), FixedClock(NOW))
    assert await worker.wait_seconds() == wait


async def test_a_failed_schedule_scan_is_logged_and_retried_after_the_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    schedule_id = UUID(int=1)
    schedules = _Schedules(due=[schedule_id], next_fire_at=NOW + timedelta(seconds=12))
    schedules.failures = 1
    clock = FixedClock(NOW)
    materialized: list[UUID] = []
    waits: list[float] = []

    async def materialize(scheduled: UUID) -> ScheduleOccurrence | None:
        materialized.append(scheduled)
        worker.stop()
        return None

    async def wakeup(seconds: float) -> None:
        waits.append(seconds)

    worker = _schedule_worker(schedules, clock, materialize=materialize, wait_for_wakeup=wakeup)
    caplog.set_level(logging.ERROR, logger="agent_core.scheduling.worker")
    await worker.run_forever()

    assert materialized == [schedule_id]
    assert waits == [12]
    assert "schedule worker scan failed" in caplog.messages


async def test_a_failed_schedule_wakeup_falls_back_to_the_bounded_sleep(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FixedClock(NOW)
    schedules = _Schedules(due=[], next_fire_at=NOW + timedelta(seconds=12))
    scans = 0

    async def broken_wakeup(_seconds: float) -> None:
        raise RuntimeError("listener connection lost")

    worker = _schedule_worker(schedules, clock, wait_for_wakeup=broken_wakeup)
    original_run_once = worker.run_once

    async def counted() -> int:
        nonlocal scans
        scans += 1
        if scans == 2:
            worker.stop()
        return await original_run_once()

    worker.run_once = counted  # type: ignore[method-assign]
    caplog.set_level(logging.ERROR, logger="agent_core.scheduling.worker")
    await worker.run_forever()

    assert scans == 2
    assert clock.now() == NOW + timedelta(seconds=12)
    assert "schedule worker wakeup failed; using poll fallback" in caplog.messages


async def test_a_failed_notification_dispatch_is_logged_and_retried_after_the_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FixedClock(NOW)
    attempts: list[datetime] = []

    async def dispatch() -> int:
        attempts.append(clock.now())
        if len(attempts) == 1:
            raise RuntimeError("transient outbox failure")
        worker.stop()
        return 1

    worker = NotificationWorker(dispatch_once=dispatch, clock=clock, fallback_poll_seconds=30)
    caplog.set_level(logging.ERROR, logger="agent_core.application.notification_worker")
    await worker.run_forever()

    assert attempts == [NOW, NOW + timedelta(seconds=30)]
    assert "notification dispatcher scan failed" in caplog.messages


async def test_a_failed_notification_wakeup_falls_back_to_the_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FixedClock(NOW)
    attempts: list[datetime] = []

    async def dispatch() -> int:
        attempts.append(clock.now())
        if len(attempts) == 2:
            worker.stop()
        return 0

    async def broken_wakeup(_seconds: float) -> None:
        raise RuntimeError("listener connection lost")

    worker = NotificationWorker(
        dispatch_once=dispatch,
        clock=clock,
        fallback_poll_seconds=30,
        wait_for_wakeup=broken_wakeup,
    )
    caplog.set_level(logging.ERROR, logger="agent_core.application.notification_worker")
    await worker.run_forever()

    assert attempts == [NOW, NOW + timedelta(seconds=30)]
    assert "notification wakeup failed; using poll fallback" in caplog.messages
