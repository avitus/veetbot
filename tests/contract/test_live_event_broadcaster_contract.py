"""The live broadcaster isolates sessions and preserves transient payloads."""

from uuid import UUID

from agent_core.adapters.live_events import InMemoryLiveEventBroadcaster

SESSION_ID = UUID("00000000-0000-0000-0000-000000000511")
OTHER_SESSION_ID = UUID("00000000-0000-0000-0000-000000000512")
RUN_ID = UUID("00000000-0000-0000-0000-000000000513")


async def test_live_broadcaster_delivers_only_to_the_subscribed_session() -> None:
    broadcaster = InMemoryLiveEventBroadcaster()
    async with broadcaster.subscribe(SESSION_ID) as subscription:
        await broadcaster.publish(OTHER_SESSION_ID, RUN_ID, "message.delta", {"text": "no"})
        assert await subscription.receive(0.001) is None
        await broadcaster.publish(SESSION_ID, RUN_ID, "message.delta", {"text": "yes"})
        notification = await subscription.receive(0.01)

    assert notification is not None
    assert notification.kind == "transient"
    assert notification.run_id == RUN_ID
    assert notification.event == "message.delta"
    assert notification.data == {"text": "yes"}
