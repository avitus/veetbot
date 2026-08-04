"""Best-effort live-event transport behind the durable event log."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LiveNotification:
    kind: str
    run_id: UUID | None = None
    sequence: int | None = None
    event: str | None = None
    data: dict[str, Any] | None = None


class LiveSubscription(Protocol):
    @property
    def overflowed(self) -> bool: ...

    async def receive(self, timeout_seconds: float) -> LiveNotification | None: ...


class LiveEventBroadcaster(Protocol):
    def subscribe(self, session_id: UUID) -> AbstractAsyncContextManager[LiveSubscription]: ...

    async def publish(
        self,
        session_id: UUID,
        run_id: UUID,
        event: str,
        data: dict[str, Any],
    ) -> None: ...

    async def close(self) -> None: ...
