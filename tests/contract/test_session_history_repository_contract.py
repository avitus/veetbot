from agent_core.adapters.persistence.memory import (
    InMemoryEventRepository,
    InMemorySessionHistoryRepository,
)
from agent_core.domain.events import NewEvent
from tests.contract.support import SESSION_ID, memory_stack


async def test_session_history_projects_event_backed_messages() -> None:
    _clock, _sessions, _runs, events = await memory_stack()
    assert isinstance(events, InMemoryEventRepository)
    history = InMemorySessionHistoryRepository(events)
    await events.append(
        NewEvent(
            session_id=SESSION_ID,
            run_id=None,
            event_type="user.message.created",
            actor_type="principal",
            actor_id="principal-a",
            payload={"content": "hello"},
        )
    )
    projected = await history.catch_up(SESSION_ID)
    assert projected.through_sequence == 1
    assert len(projected.items) == 1
