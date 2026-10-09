"""Automatic belief maintenance must not promote a quiet conversation."""

from datetime import timedelta

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.events import NewEvent
from agent_core.ports.events import EventRepository
from agent_core.ports.repositories import SessionRepository
from tests.contract.support import SESSION_ID, memory_stack, principal


async def assert_memory_maintenance_preserves_activity(
    clock: FixedClock, sessions: SessionRepository, events: EventRepository
) -> None:
    original = (await sessions.get(SESSION_ID, principal())).updated_at
    for event_type in ("memory.decayed", "memory.retired"):
        clock.advance(timedelta(days=1))
        audit = await events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type=event_type,
                actor_type="memory",
                payload={"belief_id": "retained-audit"},
            )
        )
        assert audit.created_at == clock.now()
        assert (await sessions.get(SESSION_ID, principal())).updated_at == original
    assert [event.sequence for event in await events.list_after(SESSION_ID, 0, principal())] == [
        1,
        2,
    ]
    clock.advance(timedelta(minutes=1))
    await events.append(
        NewEvent(
            session_id=SESSION_ID,
            run_id=None,
            event_type="user.message.created",
            actor_type="principal",
            payload={"content": "A real follow-up"},
        )
    )
    assert (await sessions.get(SESSION_ID, principal())).updated_at == clock.now()

    # Owner-driven memory changes still count as activity.
    clock.advance(timedelta(minutes=1))
    await events.append(
        NewEvent(
            session_id=SESSION_ID,
            run_id=None,
            event_type="memory.retired",
            actor_type="principal",
        )
    )
    assert (await sessions.get(SESSION_ID, principal())).updated_at == clock.now()


async def test_memory_maintenance_preserves_activity() -> None:
    clock, sessions, _runs, events = await memory_stack()
    await assert_memory_maintenance_preserves_activity(clock, sessions, events)
