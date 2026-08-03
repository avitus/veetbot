from agent_core.adapters.persistence.memory import InMemoryTrajectoryProjectionRepository
from agent_core.domain.events import NewEvent
from tests.contract.support import RUN_ID, SESSION_ID, memory_stack


async def test_trajectory_projection_is_rebuildable_from_run_events() -> None:
    _clock, _sessions, _runs, events = await memory_stack()
    await events.append(
        NewEvent(
            session_id=SESSION_ID,
            run_id=RUN_ID,
            event_type="run.claimed",
            actor_type="test",
        )
    )
    repository = InMemoryTrajectoryProjectionRepository(events)
    first = await repository.catch_up(RUN_ID)
    await events.append(
        NewEvent(
            session_id=SESSION_ID,
            run_id=RUN_ID,
            event_type="run.completed",
            actor_type="test",
        )
    )
    incremental = await repository.catch_up(RUN_ID)
    rebuilt = await InMemoryTrajectoryProjectionRepository(events).rebuild(RUN_ID)

    assert first is not None
    assert incremental is not None
    assert not first.terminal
    assert first.last_sequence < incremental.last_sequence
    assert incremental == rebuilt
    assert incremental.terminal
