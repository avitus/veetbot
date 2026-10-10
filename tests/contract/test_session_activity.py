"""Automatic belief maintenance must not promote a quiet conversation."""

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.ports.events import EventRepository
from agent_core.ports.repositories import SessionRepository
from tests.contract.support import SESSION_ID, memory_stack, principal, session


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


async def assert_missing_session_leaves_no_event(
    sessions: SessionRepository, events: EventRepository, event_type: str, actor_type: str
) -> None:
    missing_id = UUID(int=7167)
    event = NewEvent(
        session_id=missing_id, run_id=None, event_type=event_type, actor_type=actor_type
    )
    with pytest.raises(NotFoundError, match="session not found"):
        await events.append(event)
    await sessions.create(session().model_copy(update={"id": missing_id}))
    assert await events.list_after(missing_id, 0, principal()) == []
    assert (await events.append(event)).sequence == 1


@pytest.mark.parametrize(
    ("event_type", "actor_type"),
    [
        ("memory.decayed", "memory"),
        ("memory.retired", "memory"),
        ("user.message.created", "principal"),
    ],
)
async def test_missing_session_append_is_atomic(event_type: str, actor_type: str) -> None:
    _clock, sessions, _runs, events = await memory_stack()
    await assert_missing_session_leaves_no_event(sessions, events, event_type, actor_type)
