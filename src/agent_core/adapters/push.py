"""Deterministic push transport used by application and contract tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from agent_core.domain.devices import PushTarget
from agent_core.domain.notifications import (
    DeliveryOutcome,
    PushMessage,
    PushOutcome,
)


class FakePushTransport:
    """Return scripted outcomes and retain bounded call metadata for assertions."""

    def __init__(self, outcomes: Iterable[PushOutcome] = ()) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[tuple[PushTarget, PushMessage]] = []

    async def deliver(self, target: PushTarget, message: PushMessage) -> PushOutcome:
        self.calls.append((target.model_copy(deep=True), message.model_copy(deep=True)))
        if self._outcomes:
            return self._outcomes.popleft()
        return PushOutcome(outcome=DeliveryOutcome.DELIVERED, provider_id="fake-delivery")
