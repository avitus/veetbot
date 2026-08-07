"""The bounded live subscription exposes timeout and overflow semantics."""

from uuid import UUID

from agent_core.adapters.live_events import LIVE_QUEUE_SIZE, InMemoryLiveEventBroadcaster

SESSION_ID = UUID("00000000-0000-0000-0000-000000000501")
RUN_ID = UUID("00000000-0000-0000-0000-000000000502")


async def test_live_subscription_times_out_and_reports_overflow() -> None:
    broadcaster = InMemoryLiveEventBroadcaster()
    async with broadcaster.subscribe(SESSION_ID) as subscription:
        assert await subscription.receive(0.001) is None
        for index in range(LIVE_QUEUE_SIZE + 1):
            await broadcaster.publish(SESSION_ID, RUN_ID, "message.delta", {"index": index})
        assert subscription.overflowed
