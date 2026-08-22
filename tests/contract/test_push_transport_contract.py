"""Shared push-transport contract exercised when transport adapters land."""

from __future__ import annotations

from agent_core.domain.devices import PushTarget
from agent_core.domain.notifications import PushMessage
from agent_core.ports.notifications import PushTransport


async def assert_push_transport_returns_a_closed_outcome(
    transport: PushTransport,
    target: PushTarget,
    message: PushMessage,
) -> None:
    outcome = await transport.deliver(target, message)
    assert outcome.outcome.value in {
        "delivered",
        "retry",
        "unregistered",
        "rejected",
        "skipped",
    }
