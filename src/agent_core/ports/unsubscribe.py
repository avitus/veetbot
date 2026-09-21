"""One-click unsubscribe transport port."""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.unsubscribe import UnsubscribeOutcomeCode


class OneClickTransport(Protocol):
    """Send the fixed RFC 8058 request; remote and network failures are closed codes."""

    async def post(self, url: str) -> UnsubscribeOutcomeCode: ...

    async def close(self) -> None: ...
