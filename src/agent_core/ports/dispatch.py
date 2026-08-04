"""Run dispatch and cooperative cancellation ports."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.persistence import ClaimedRun, WorkerLease
from agent_core.domain.runs import CancelReason, Run, RunStatus


class RunDispatcher(Protocol):
    async def dispatch(self, run_id: UUID) -> None:
        """Guarantee that the committed run will be executed exactly once."""
        ...

    async def resume(self, run_id: UUID) -> None:
        """Dispatch a new queued generation of a previously parked run."""
        ...


class RunQueue(Protocol):
    async def enqueue(self, run: Run, *, priority: int, scheduled_for: datetime | None) -> None: ...

    async def claim(self, worker_id: str, eligible_classes: Sequence[int]) -> ClaimedRun | None: ...

    async def heartbeat(self, lease: WorkerLease) -> bool: ...

    async def release(self, lease: WorkerLease, status: RunStatus) -> None: ...

    async def reclaim_expired(self, limit: int) -> int: ...


class WorkerService(Protocol):
    def stop(self) -> None: ...

    async def run_forever(self) -> None: ...


class CancellationToken(Protocol):
    @property
    def reason(self) -> CancelReason | None: ...

    def raise_if_cancelled(self) -> None: ...

    async def wait(self) -> CancelReason: ...
