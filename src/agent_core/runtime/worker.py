"""Durable worker and maintenance roles over the PostgreSQL queue port."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from time import perf_counter
from uuid import UUID

from agent_core.domain.errors import ArtifactSweepError
from agent_core.domain.events import NewEvent
from agent_core.domain.persistence import ClaimedRun
from agent_core.domain.runs import CancelReason
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.executor import RunExecutor

logger = logging.getLogger(__name__)

type LeaseLiveCheck = Callable[[UUID, int], Awaitable[bool]]
type SandboxSweep = Callable[[frozenset[tuple[UUID, int]], LeaseLiveCheck], Awaitable[int]]
type RecordClaimMetric = Callable[[str, float], None]


class DurableWorker:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        executor: RunExecutor,
        clock: Clock,
        worker_id: str,
        eligible_classes: Sequence[int] = (0, 10),
        interactive_priority: int = 0,
        async_priority: int = 10,
        lease_seconds: float = 30,
        heartbeat_divisor: int = 3,
        poll_interval_seconds: float = 0.25,
        record_claim_metric: RecordClaimMetric | None = None,
    ) -> None:
        if heartbeat_divisor < 2:
            raise ValueError("heartbeat_divisor must be at least two")
        self._uow_factory = uow_factory
        self._executor = executor
        self._clock = clock
        self._worker_id = worker_id
        self._eligible_classes = tuple(eligible_classes)
        self._worker_class = (
            "interactive"
            if self._eligible_classes == (interactive_priority,)
            else "async"
            if self._eligible_classes == (async_priority,)
            else "other"
        )
        self._heartbeat_interval = lease_seconds / heartbeat_divisor
        self._poll_interval = poll_interval_seconds
        self._record_claim_metric = record_claim_metric
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def claim(self) -> ClaimedRun | None:
        started = perf_counter()
        async with self._uow_factory() as uow:
            if uow.queue is None:
                raise RuntimeError("durable worker requires a queue repository")
            claimed = await uow.queue.claim(self._worker_id, self._eligible_classes)
            if claimed is not None:
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
        if self._record_claim_metric is not None:
            try:
                self._record_claim_metric(self._worker_class, perf_counter() - started)
            except Exception:
                logger.warning("worker_claim_metric_failed", exc_info=True)
        return claimed

    async def run_once(self) -> bool:
        claimed = await self.claim()
        if claimed is None:
            return False
        token: RunCancellationToken | None = None
        cancellation_pending = False

        def capture(run_id: object, active: RunCancellationToken) -> None:
            nonlocal token
            if run_id != claimed.run.id:
                raise RuntimeError("worker received a cancellation token for another run")
            token = active
            if cancellation_pending:
                token.cancel(CancelReason.REQUESTED)

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
                    owned, cancellation_requested = await uow.queue.heartbeat(claimed.lease)
                if not owned:
                    if token is not None:
                        token.cancel(CancelReason.FENCED)
                    else:
                        execution.cancel()
                        cancelled_without_token = True
                    break
                if cancellation_requested:
                    cancellation_pending = True
                    if token is not None:
                        token.cancel(CancelReason.REQUESTED)
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
        sweep_artifacts: Callable[[], Awaitable[int]] | None = None,
        sweep_sandboxes: SandboxSweep | None = None,
        sweep_artifact_orphans: Callable[[], Awaitable[int]] | None = None,
        sweep_memory: Callable[[], Awaitable[int]] | None = None,
        sweep_traces: Callable[[], Awaitable[int]] | None = None,
        sweep_memory_consolidation: Callable[[], Awaitable[int]] | None = None,
        sweep_memory_decay: Callable[[], Awaitable[int]] | None = None,
        sweep_session_deletions: Callable[[], Awaitable[int]] | None = None,
        artifact_orphan_interval_seconds: float = 3600,
        memory_decay_interval_seconds: float = 86_400,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._poll_interval = poll_interval_seconds
        self._reclaim_limit = reclaim_limit
        self._sweep_exports = sweep_exports
        self._sweep_artifacts = sweep_artifacts
        self._sweep_sandboxes = sweep_sandboxes
        self._sweep_artifact_orphans = sweep_artifact_orphans
        self._sweep_memory = sweep_memory
        self._sweep_traces = sweep_traces
        self._sweep_memory_consolidation = sweep_memory_consolidation
        self._sweep_memory_decay = sweep_memory_decay
        self._sweep_session_deletions = sweep_session_deletions
        if artifact_orphan_interval_seconds <= 0:
            raise ValueError("artifact orphan interval must be positive")
        if memory_decay_interval_seconds <= 0:
            raise ValueError("memory decay interval must be positive")
        self._artifact_orphan_interval = timedelta(seconds=artifact_orphan_interval_seconds)
        self._last_artifact_orphan_sweep_at: datetime | None = None
        # Decay is a slow sweep on its own timer: the maintenance pass runs
        # every few seconds, and a belief may lose one step per interval.
        self._memory_decay_interval = timedelta(seconds=memory_decay_interval_seconds)
        self._last_memory_decay_sweep_at: datetime | None = None
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_once(self) -> int:
        async with self._uow_factory() as uow:
            reclaimed = (
                0 if uow.queue is None else await uow.queue.reclaim_expired(self._reclaim_limit)
            )
            live_run_leases = await uow.maintenance.live_run_leases()
        if self._sweep_sandboxes is not None:

            async def lease_is_live(run_id: UUID, lease_epoch: int) -> bool:
                async with self._uow_factory() as uow:
                    return await uow.maintenance.is_live_run_lease(run_id, lease_epoch)

            try:
                await self._sweep_sandboxes(live_run_leases, lease_is_live)
            except Exception:
                logger.exception("sandbox reaper failed")
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
        if self._sweep_artifacts is not None:
            try:
                await self._sweep_artifacts()
            except Exception:
                logger.exception("general artifact expiry sweep failed")
        if self._sweep_memory is not None:
            try:
                await self._sweep_memory()
            except Exception:
                logger.exception("memory expiry sweep failed")
        if self._sweep_traces is not None:
            try:
                await self._sweep_traces()
            except Exception:
                logger.exception("recall trace operator-field expiry sweep failed")
        if self._sweep_memory_consolidation is not None:
            try:
                await self._sweep_memory_consolidation()
            except Exception:
                logger.exception("memory consolidation sweep failed")
        decay_sweep_due = (
            self._last_memory_decay_sweep_at is None
            or self._clock.now() - self._last_memory_decay_sweep_at >= self._memory_decay_interval
        )
        if self._sweep_memory_decay is not None and decay_sweep_due:
            self._last_memory_decay_sweep_at = self._clock.now()
            try:
                await self._sweep_memory_decay()
            except Exception:
                logger.exception("memory decay sweep failed")
        if self._sweep_session_deletions is not None:
            try:
                await self._sweep_session_deletions()
            except Exception:
                logger.exception("session artifact deletion retry failed")
        orphan_sweep_due = (
            self._last_artifact_orphan_sweep_at is None
            or self._clock.now() - self._last_artifact_orphan_sweep_at
            >= self._artifact_orphan_interval
        )
        if self._sweep_artifact_orphans is not None and orphan_sweep_due:
            self._last_artifact_orphan_sweep_at = self._clock.now()
            try:
                await self._sweep_artifact_orphans()
            except Exception:
                logger.exception("artifact orphan reconciliation failed")
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
