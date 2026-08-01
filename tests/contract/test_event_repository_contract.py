from agent_core.domain.events import NewEvent
from tests.contract.support import SESSION_ID, memory_stack, principal


async def test_event_repository_assigns_gapless_per_session_sequences() -> None:
    _clock, _sessions, _runs, repository = await memory_stack()
    for event_type in ("first", "second"):
        await repository.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type=event_type,
                actor_type="contract",
            )
        )
    events = await repository.list_after(SESSION_ID, 0, principal())
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == ["first", "second"]
