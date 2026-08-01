"""Run dispatch and cooperative cancellation ports."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from agent_core.domain.runs import CancelReason


class RunDispatcher(Protocol):
    async def dispatch(self, run_id: UUID) -> None:
        """Guarantee that the committed run will be executed exactly once."""
        ...


class CancellationToken(Protocol):
    @property
    def reason(self) -> CancelReason | None: ...

    def raise_if_cancelled(self) -> None: ...

    async def wait(self) -> CancelReason: ...
