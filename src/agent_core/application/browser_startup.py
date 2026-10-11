"""Bounded recovery of idempotent hosted lease acquisition, never browser effects."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import cast

from agent_core.domain.browser import BrowserLease, BrowserProviderError
from agent_core.domain.browser_diagnostics import browser_phase
from agent_core.domain.tools import ToolExecutionContext
from agent_core.ports.determinism import Clock
from agent_core.ports.dispatch import CancellationToken


class BrowserStartupRecovery:
    """Keep up to three acquisition attempts inside the original call deadline."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def __call__(
        self,
        acquire: Callable[[], Awaitable[BrowserLease]],
        context: ToolExecutionContext,
    ) -> BrowserLease:
        """Retry only acquisition with an identical request and finite backoff."""
        token = cast(CancellationToken, context.cancellation)
        deadline = min(context.deadline_at, context.run_deadline_at or context.deadline_at)
        token.raise_if_cancelled()
        remaining = (deadline - self._clock.now()).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        async with asyncio.timeout(remaining):
            for attempt in range(3):
                token.raise_if_cancelled()
                if self._clock.now() >= deadline:
                    raise TimeoutError
                try:
                    with browser_phase("acquisition", "orchestrator"):
                        return await acquire()
                except BrowserProviderError as error:
                    if (
                        error.reason_code != "tool.browser.provider_unavailable"
                        or not error.retryable
                        or attempt == 2
                    ):
                        raise
                    ceiling = 0.5 * 2**attempt
                    # Stable per invocation/attempt, without an ambient random source.
                    digest = hashlib.sha256(f"{context.invocation_id}:{attempt}".encode()).digest()
                    delay = ceiling / 2 * (1 + digest[0] / 255)
                    token.raise_if_cancelled()
                    if (deadline - self._clock.now()).total_seconds() <= delay:
                        raise
                    await self._clock.sleep(delay)
        raise AssertionError("acquisition attempts must return or raise")
