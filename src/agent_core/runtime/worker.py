"""Durable worker and maintenance roles over the PostgreSQL queue port."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from agent_core.domain.errors import ArtifactSweepError
from agent_core.domain.events import NewEvent
from agent_core.domain.persistence import ClaimedRun
from agent_core.domain.runs import CancelReason
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.executor import RunExecutor

logger = logging.getLogger(__name__)


class DurableWorker:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        executor: RunExecutor,
        clock: Clock,
        worker_id: str,
        eligible_classes: Sequence[int] = (0, 10),
        lease_seconds: float = 30,
        heartbeat_divisor: int = 3,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        if heartbeat_divisor < 2:
            raise ValueError("heartbeat_divisor must be at least two")
        self._uow_factory = uow_factory
        self._executor = executor
        self._clock = clock
        self._worker_id = worker_id
        self._eligible_classes = tuple(eligible_classes)
        self._heartbeat_interval = lease_seconds / heartbeat_divisor
        self._poll_interval = poll_interval_seconds
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def claim(self) -> ClaimedRun | None:
        async with self._uow_factory() as uow:
            if uow.queue is None:
                raise RuntimeError("durable worker requires a queue repository")
            claimed = await uow.queue.claim(self._worker_id, self._eligible_classes)
            if claimed is None:
                return None
            await uow.events.append(
                NewEvent(
                    session_id=claimed.run.session_id,
                    run_id=claimed.run.id,
                    event_type="run.claimed",
                    actor_type="worker",
                    actor_id=self._worker_id,
                    payload={
                        "worker_id": self._worker_id,
                        "lease_epoch": claimed.lease.lease_epoch,
                        "attempt": claimed.run.attempts,
                    },
                ),
                lease=claimed.lease,
            )
            await uow.events.append(
                NewEvent(
                    session_id=claimed.run.session_id,
                    run_id=claimed.run.id,
                    event_type="run.started",
                    actor_type="worker",
                    actor_id=self._worker_id,
                    payload={
                        "lease_epoch": claimed.lease.lease_epoch,
                        "attempt": claimed.run.attempts,
                    },
                ),
                lease=claimed.lease,
            )
            return claimed

    async def run_once(self) -> bool:
        claimed = await self.claim()
        if claimed is None:
            return False
        token: RunCancellationToken | None = None

        def capture(active: RunCancellationToken) -> None:
            nonlocal token
            token = active

        execution = asyncio.create_task(
            self._executor.execute_claimed(claimed, on_token=capture),
            name=f"run-{claimed.run.id}",
        )
        cancelled_without_token = False
        try:
            while not execution.done():
                sleeper = asyncio.create_task(self._clock.sleep(self._heartbeat_interval))
                done, _pending = await asyncio.wait(
                    {execution, sleeper}, return_when=asyncio.FIRST_COMPLETED
                )
                if execution in done:
                    sleeper.cancel()
                    await asyncio.gather(sleeper, return_exceptions=True)
                    break
                async with self._uow_factory() as uow:
                    if uow.queue is None:
                        raise RuntimeError("durable worker requires a queue repository")
                    owned = await uow.queue.heartbeat(claimed.lease)
                if not owned:
                    if token is not None:
                        token.cancel(CancelReason.FENCED)
                    else:
                        execution.cancel()
                        cancelled_without_token = True
                    break
            if cancelled_without_token:
                await asyncio.gather(execution, return_exceptions=True)
            else:
                await execution
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
        return True

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("durable worker iteration failed: %s", self._worker_id)
                await self._clock.sleep(self._poll_interval)
                continue
            if not worked:
                await self._clock.sleep(self._poll_interval)


class MaintenanceWorker:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        poll_interval_seconds: float = 5,
        reclaim_limit: int = 100,
        sweep_exports: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._poll_interval = poll_interval_seconds
        self._reclaim_limit = reclaim_limit
        self._sweep_exports = sweep_exports
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_once(self) -> int:
        async with self._uow_factory() as uow:
            if uow.queue is None:
                raise RuntimeError("maintenance worker requires a queue repository")
            reclaimed = await uow.queue.reclaim_expired(self._reclaim_limit)
        async with self._uow_factory() as uow:
            sessions = await uow.maintenance.projection_sessions(self._reclaim_limit)
        for session_id in sessions:
            async with self._uow_factory() as uow:
                await uow.history.catch_up(session_id)
        async with self._uow_factory() as uow:
            trajectories = await uow.maintenance.trajectory_runs(self._reclaim_limit)
        for run_id in trajectories:
            async with self._uow_factory() as uow:
                await uow.trajectory.catch_up(run_id)
        async with self._uow_factory() as uow:
            checkpoints = await uow.maintenance.checkpoint_runs(self._reclaim_limit)
        for run_id, terminal in checkpoints:
            async with self._uow_factory() as uow:
                await uow.checkpoints.prune(run_id, terminal=terminal)
        if self._sweep_exports is not None:
            try:
                await self._sweep_exports()
            except ArtifactSweepError:
                logger.exception("trajectory artifact sweep failed")
        return reclaimed

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("maintenance worker iteration failed")
            await self._clock.sleep(self._poll_interval)
