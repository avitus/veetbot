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


async def test_event_repository_reads_an_idempotent_derivation_key() -> None:
    _clock, _sessions, _runs, repository = await memory_stack()
    event = NewEvent(
        session_id=SESSION_ID,
        run_id=None,
        event_type="derived",
        actor_type="contract",
        derivation_key="contract:derived",
    )
    first = await repository.append(event)
    second = await repository.append(event)
    loaded = await repository.get_by_derivation("contract:derived", principal())
    assert loaded == first == second
    assert await repository.get_by_derivation("contract:missing", principal()) is None


async def test_event_repository_reads_the_latest_typed_event_before_a_sequence() -> None:
    _clock, _sessions, _runs, repository = await memory_stack()
    for event_type in (
        "context.working_state.updated",
        "other",
        "context.working_state.updated",
        "context.working_state.updated",
    ):
        await repository.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type=event_type,
                actor_type="contract",
            )
        )

    latest = await repository.latest_before(
        SESSION_ID,
        4,
        "context.working_state.updated",
        principal(),
    )

    assert latest is not None
    assert latest.sequence == 3
    assert (
        await repository.latest_before(
            SESSION_ID,
            1,
            "context.working_state.updated",
            principal(),
        )
        is None
    )
