"""PostgreSQL run queue with leases, fencing, and reclaim."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.mappers import run_to_domain, run_values
from agent_core.adapters.persistence.repositories import (
    PostgresEventRepository,
    execute_run_insert,
)
from agent_core.adapters.persistence.sqlalchemy_models import RunRow
from agent_core.domain.errors import ConflictError, WorkerFencedError
from agent_core.domain.events import NewEvent
from agent_core.domain.persistence import ClaimedRun, WorkerLease
from agent_core.domain.runs import FailureReason, Run, RunFailure, RunStatus
from agent_core.ports.determinism import Clock
from agent_core.runtime.state_machine import require_transition


def _rowcount(result: Any) -> int:
    return int(result.rowcount or 0)


class PostgresRunQueue:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        events: PostgresEventRepository,
        *,
        lease_seconds: float,
        max_attempts: int,
    ) -> None:
        self._session = session
        self._clock = clock
        self._events = events
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def enqueue(self, run: Run, *, priority: int, scheduled_for: datetime | None) -> None:
        values = run_values(
            run.model_copy(update={"priority": priority, "scheduled_for": scheduled_for}, deep=True)
        )
        statement = (
            pg_insert(RunRow).values(**values).on_conflict_do_nothing(index_elements=[RunRow.id])
        )
        if not await execute_run_insert(self._session, statement):
            raise ConflictError("run already exists")

    async def claim(self, worker_id: str, eligible_classes: Sequence[int]) -> ClaimedRun | None:
        classes = list(eligible_classes)
        if not classes:
            return None
        now = self._clock.now()
        candidate = (
            select(RunRow.id)
            .where(
                RunRow.status == RunStatus.QUEUED.value,
                RunRow.priority.in_(classes),
                (RunRow.scheduled_for.is_(None) | (RunRow.scheduled_for <= now)),
                RunRow.attempts < self._max_attempts,
            )
            .order_by(RunRow.priority, RunRow.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(RunRow)
            .where(RunRow.id == candidate)
            .values(
                status=RunStatus.RUNNING.value,
                lease_owner=worker_id,
                lease_epoch=RunRow.lease_epoch + 1,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                attempts=RunRow.attempts + 1,
                scheduled_for=None,
                failure=None,
                updated_at=now,
            )
            .returning(RunRow)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            return None
        run = run_to_domain(row)
        lease = WorkerLease(run_id=run.id, worker_id=worker_id, lease_epoch=run.lease_epoch)
        return ClaimedRun(run=run, lease=lease)

    async def heartbeat(self, lease: WorkerLease) -> bool:
        statement = (
            update(RunRow)
            .where(
                RunRow.id == lease.run_id,
                RunRow.status == RunStatus.RUNNING.value,
                RunRow.lease_owner == lease.worker_id,
                RunRow.lease_epoch == lease.lease_epoch,
            )
            .values(
                lease_expires_at=self._clock.now() + timedelta(seconds=self._lease_seconds),
                updated_at=self._clock.now(),
            )
        )
        return bool(_rowcount(await self._session.execute(statement)))

    async def release(self, lease: WorkerLease, status: RunStatus) -> None:
        statement = (
            update(RunRow)
            .where(
                RunRow.id == lease.run_id,
                RunRow.status == status.value,
                RunRow.lease_owner == lease.worker_id,
                RunRow.lease_epoch == lease.lease_epoch,
            )
            .values(lease_owner=None, lease_expires_at=None, updated_at=self._clock.now())
        )
        if not _rowcount(await self._session.execute(statement)):
            current = (
                await self._session.execute(
                    select(RunRow.lease_owner, RunRow.lease_epoch, RunRow.status).where(
                        RunRow.id == lease.run_id
                    )
                )
            ).one_or_none()
            if current is None:
                raise ConflictError("lease release run does not exist")
            if current.lease_owner != lease.worker_id or current.lease_epoch != lease.lease_epoch:
                raise WorkerFencedError("lease release guard failed; worker was fenced")
            raise ConflictError(
                f"lease release expected run status {status.value}, found {current.status}"
            )

    async def reclaim_expired(self, limit: int) -> int:
        acquired = await self._session.scalar(
            select(
                func.pg_try_advisory_xact_lock(func.hashtextextended("maintenance.run_reclaim", 0))
            )
        )
        if not acquired:
            return 0
        now = self._clock.now()
        rows = list(
            (
                await self._session.scalars(
                    select(RunRow)
                    .where(
                        RunRow.status == RunStatus.RUNNING.value,
                        RunRow.lease_expires_at.is_not(None),
                        RunRow.lease_expires_at <= now,
                    )
                    .order_by(RunRow.lease_expires_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        for row in rows:
            previous_epoch = row.lease_epoch
            if row.attempts >= self._max_attempts:
                require_transition(RunStatus.RUNNING, RunStatus.FAILED)
                failure = RunFailure(
                    reason=FailureReason.MAX_ATTEMPTS_EXCEEDED,
                    error_class="MaxAttemptsExceeded",
                    message="the durable worker attempt limit was reached",
                    attempt_number=row.attempts,
                    occurred_at=now,
                )
                row.status = RunStatus.FAILED.value
                row.failure = failure.model_dump(mode="json")
                event_type = "run.failed"
                payload = {"failure": row.failure, "reclaimed_epoch": previous_epoch}
            else:
                require_transition(RunStatus.RUNNING, RunStatus.QUEUED)
                row.status = RunStatus.QUEUED.value
                row.scheduled_for = now + timedelta(seconds=2 ** max(0, row.attempts - 1))
                event_type = "run.requeued"
                payload = {"reclaimed_epoch": previous_epoch, "attempts": row.attempts}
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = now
            await self._events.append(
                NewEvent(
                    session_id=row.session_id,
                    run_id=row.id,
                    event_type=event_type,
                    actor_type="maintenance",
                    payload=payload,
                )
            )
        return len(rows)
