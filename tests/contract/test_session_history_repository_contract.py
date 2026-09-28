from agent_core.adapters.persistence.memory import (
    InMemoryEventRepository,
    InMemorySessionHistoryRepository,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import TextPart, UserMessage
from tests.contract.support import SESSION_ID, memory_stack


def _event(event_type: str, payload: dict[str, object]) -> NewEvent:
    return NewEvent(
        session_id=SESSION_ID,
        run_id=None,
        event_type=event_type,
        actor_type="principal",
        actor_id="principal-a",
        payload=payload,
    )


async def test_session_history_projects_event_backed_messages() -> None:
    _clock, _sessions, _runs, events = await memory_stack()
    assert isinstance(events, InMemoryEventRepository)
    history = InMemorySessionHistoryRepository(events)
    await events.append(_event("user.message.created", {"content": "hello"}))
    projected = await history.catch_up(SESSION_ID)
    assert projected.through_sequence == 1
    assert len(projected.items) == 1
    [message] = projected.items
    assert isinstance(message, UserMessage)
    assert message.content == [TextPart(text="hello")]
    assert message.principal_id == "principal-a"
    assert message.source_event_sequence == 1


async def test_session_history_covers_non_message_events_and_reads_a_prefix() -> None:
    _clock, _sessions, _runs, events = await memory_stack()
    assert isinstance(events, InMemoryEventRepository)
    history = InMemorySessionHistoryRepository(events)
    await events.append(_event("user.message.created", {"content": "first"}))
    await events.append(_event("run.queued", {}))
    await events.append(_event("user.message.created", {"content": "second"}))

    projected = await history.catch_up(SESSION_ID)
    prefix = await history.read(SESSION_ID, 2)

    assert projected.through_sequence == 3
    assert [item.source_event_sequence for item in projected.items] == [1, 3]
    assert await history.rebuild(SESSION_ID) == projected
    assert await history.read(SESSION_ID) == projected
    assert prefix.through_sequence == 2
    assert [item.source_event_sequence for item in prefix.items] == [1]
