"""The durable worker's heartbeat supervises a claimed run from outside the loop.

runtime-loop.md, "The heartbeat is a supervisor": the lease is refreshed on a
timer independent of what the loop is doing, at one third of the lease; a
heartbeat that reports the lease lost cancels the run's token with FENCED, and
a heartbeat that reports a cancellation request cancels it with REQUESTED.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import Any, cast

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.events import NewEvent
from agent_core.domain.persistence import ClaimedRun, WorkerLease
from agent_core.domain.runs import CancelReason, RunStatus
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.executor import RunExecutor
from agent_core.runtime.worker import DurableWorker
from tests.contract.support import NOW, RUN_ID, run

LEASE = WorkerLease(run_id=RUN_ID, worker_id="worker-1", lease_epoch=7)
CLAIMED = ClaimedRun(
    run=run(status=RunStatus.RUNNING).model_copy(update={"attempts": 2}), lease=LEASE
)
type Beat = tuple[bool, bool]


class _RecordingClock(FixedClock):
    def __init__(self) -> None:
        super().__init__(NOW)
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


class _Queue:
    def __init__(self, claimed: ClaimedRun | None, beats: list[Beat]) -> None:
        self._claimed = claimed
        self._beats = beats
        self.heartbeats: list[WorkerLease] = []
        self.claims: list[tuple[str, tuple[int, ...]]] = []

    async def claim(self, worker_id: str, classes: tuple[int, ...]) -> ClaimedRun | None:
        self.claims.append((worker_id, classes))
        claimed, self._claimed = self._claimed, None
        return claimed

    async def heartbeat(self, lease: WorkerLease) -> Beat:
        self.heartbeats.append(lease)
        return self._beats.pop(0) if self._beats else (True, False)


class _Events:
    def __init__(self) -> None:
        self.appended: list[tuple[NewEvent, WorkerLease | None]] = []

    async def append(self, event: NewEvent, *, lease: WorkerLease | None = None) -> None:
        self.appended.append((event, lease))


class _UnitOfWork:
    def __init__(self, queue: _Queue) -> None:
        self.queue = queue
        self.events = _Events()

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _Executor:
    """Runs a scripted body in place of the run loop and records what it saw."""

    def __init__(self, clock: FixedClock, body: str, queue: _Queue) -> None:
        self._clock = clock
        self._body = body
        self._queue = queue
        self.observed: CancelReason | None = None
        self.interrupted = False
        self.calls = 0

    async def execute_claimed(
        self,
        claimed: ClaimedRun,
        *,
        on_token: Callable[[object, RunCancellationToken], None],
    ) -> None:
        self.calls += 1
        token = RunCancellationToken(self._clock, None)
        try:
            if self._body == "token_then_wait":
                on_token(claimed.run.id, token)
                self.observed = await token.wait()
            elif self._body == "token_after_first_heartbeat":
                while not self._queue.heartbeats:
                    await asyncio.sleep(0)
                on_token(claimed.run.id, token)
                self.observed = await token.wait()
            elif self._body == "no_token_forever":
                await asyncio.Event().wait()
            elif self._body == "three_heartbeats":
                on_token(claimed.run.id, token)
                while len(self._queue.heartbeats) < 3:
                    await asyncio.sleep(0)
                self.observed = token.reason
        except asyncio.CancelledError:
            self.interrupted = True
            raise


def _worker(
    body: str,
    beats: list[Beat],
    *,
    claimed: ClaimedRun | None = CLAIMED,
    **options: Any,
) -> tuple[DurableWorker, _Executor, _Queue, _UnitOfWork, _RecordingClock]:
    clock = _RecordingClock()
    queue = _Queue(claimed, beats)
    unit = _UnitOfWork(queue)
    executor = _Executor(clock, body, queue)
    worker = DurableWorker(
        uow_factory=cast(UnitOfWorkFactory, lambda: unit),
        executor=cast(RunExecutor, executor),
        clock=clock,
        worker_id="worker-1",
        lease_seconds=30,
        **options,
    )
    return worker, executor, queue, unit, clock


async def test_a_lost_lease_fences_the_running_loop_through_its_token() -> None:
    worker, executor, queue, _unit, _clock = _worker("token_then_wait", [(False, False)])

    assert await worker.run_once() is True

    assert executor.observed is CancelReason.FENCED
    assert executor.interrupted is False
    assert queue.heartbeats == [LEASE]


async def test_a_lease_lost_before_the_loop_has_a_token_cancels_the_execution() -> None:
    worker, executor, queue, _unit, _clock = _worker("no_token_forever", [(False, False)])

    assert await worker.run_once() is True

    assert executor.interrupted is True
    assert queue.heartbeats == [LEASE]


async def test_a_cancellation_request_reaches_the_loop_as_requested() -> None:
    worker, executor, _queue, _unit, _clock = _worker("token_then_wait", [(True, True)])

    assert await worker.run_once() is True

    assert executor.observed is CancelReason.REQUESTED


async def test_a_cancellation_seen_before_the_token_exists_is_applied_when_it_arrives() -> None:
    worker, executor, queue, _unit, _clock = _worker("token_after_first_heartbeat", [(True, True)])

    assert await worker.run_once() is True

    assert executor.observed is CancelReason.REQUESTED
    assert executor.interrupted is False
    assert queue.heartbeats[0] == LEASE


async def test_a_healthy_lease_is_renewed_every_third_of_its_duration_without_cancelling() -> None:
    worker, executor, queue, _unit, clock = _worker("three_heartbeats", [])

    assert await worker.run_once() is True

    assert executor.observed is None
    assert len(queue.heartbeats) >= 3
    assert all(lease == LEASE for lease in queue.heartbeats)
    assert clock.sleeps and set(clock.sleeps) == {10.0}


async def test_a_claim_is_announced_under_its_lease_before_execution() -> None:
    worker, executor, queue, unit, _clock = _worker(
        "token_then_wait", [(True, True)], eligible_classes=(0,)
    )

    assert await worker.run_once() is True

    assert queue.claims == [("worker-1", (0,))]
    assert [event.event_type for event, _lease in unit.events.appended] == [
        "run.claimed",
        "run.started",
    ]
    assert [lease for _event, lease in unit.events.appended] == [LEASE, LEASE]
    claimed_event, started_event = (event for event, _lease in unit.events.appended)
    assert claimed_event.payload == {"worker_id": "worker-1", "lease_epoch": 7, "attempt": 2}
    assert started_event.payload == {"lease_epoch": 7, "attempt": 2}
    assert executor.calls == 1


async def test_an_empty_queue_reports_no_work_and_starts_nothing() -> None:
    metrics: list[tuple[str, float]] = []
    worker, executor, queue, unit, _clock = _worker(
        "token_then_wait",
        [],
        claimed=None,
        eligible_classes=(10,),
        record_claim_metric=lambda worker_class, seconds: metrics.append((worker_class, seconds)),
    )

    assert await worker.run_once() is False

    assert executor.calls == 0
    assert unit.events.appended == []
    assert queue.heartbeats == []
    assert [worker_class for worker_class, _seconds in metrics] == ["async"]


async def test_a_failing_claim_metric_does_not_lose_the_claimed_run() -> None:
    def broken(worker_class: str, seconds: float) -> None:
        del worker_class, seconds
        raise RuntimeError("metrics backend unavailable")

    worker, executor, _queue, _unit, _clock = _worker(
        "token_then_wait", [(True, True)], record_claim_metric=broken
    )

    assert await worker.run_once() is True
    assert executor.calls == 1


async def test_run_forever_survives_a_failed_iteration_and_stops_on_request() -> None:
    worker, _executor, queue, _unit, clock = _worker("token_then_wait", [], claimed=None)
    attempts = 0
    original_claim = queue.claim

    async def flaky_claim(worker_id: str, classes: tuple[int, ...]) -> ClaimedRun | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database restarting")
        worker.stop()
        return await original_claim(worker_id, classes)

    queue.claim = flaky_claim  # type: ignore[method-assign]

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert attempts == 2
    assert clock.sleeps == [0.25, 0.25]


@pytest.mark.parametrize(
    "options",
    [{"heartbeat_divisor": 1}, {"browser_lease_upkeep_seconds": 0}],
    ids=["heartbeat_divisor_below_two", "non_positive_upkeep"],
)
def test_worker_rejects_supervision_settings_that_cannot_keep_a_lease(
    options: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _worker("token_then_wait", [], **options)
