"""One-shot cooperative cancellation with a lazily observed deadline."""

from __future__ import annotations

import asyncio
from datetime import datetime

from agent_core.domain.errors import RunCancelledError
from agent_core.domain.runs import CancelReason
from agent_core.ports.determinism import Clock


class RunCancellationToken:
    def __init__(self, clock: Clock, deadline_at: datetime | None) -> None:
        self._clock = clock
        self._deadline_at = deadline_at
        self._reason: CancelReason | None = None
        self._event = asyncio.Event()

    @property
    def reason(self) -> CancelReason | None:
        if (
            self._reason is None
            and self._deadline_at is not None
            and self._clock.now() >= self._deadline_at
        ):
            self.cancel(CancelReason.DEADLINE)
        return self._reason

    def cancel(self, reason: CancelReason) -> None:
        if self._reason is None:
            self._reason = reason
            self._event.set()

    def raise_if_cancelled(self) -> None:
        reason = self.reason
        if reason is not None:
            raise RunCancelledError(reason.value)

    async def wait(self) -> CancelReason:
        reason = self.reason
        if reason is not None:
            return reason
        await self._event.wait()
        if self._reason is None:
            raise RuntimeError("cancellation event set without a reason")
        return self._reason
