from datetime import timedelta

import pytest

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
    with pytest.raises(ValueError, match="nonnegative"):
        await repository.list_after(SESSION_ID, 0, principal(), limit=-1)


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


async def test_event_append_never_regresses_session_activity_time() -> None:
    clock, sessions, _runs, repository = await memory_stack()
    clock.advance(-timedelta(minutes=1))

    await repository.append(
        NewEvent(
            session_id=SESSION_ID,
            run_id=None,
            event_type="late-observed",
            actor_type="contract",
        )
    )

    assert (await sessions.get(SESSION_ID, principal())).updated_at > clock.now()


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
    assert await repository.existing_sequences(SESSION_ID, {1, 3, 99}, principal()) == {
        1,
        3,
    }
    assert (
        await repository.latest_before(
            SESSION_ID,
            1,
            "context.working_state.updated",
            principal(),
        )
        is None
    )


async def test_event_repository_bounds_conversation_event_queries() -> None:
    _clock, _sessions, _runs, repository = await memory_stack()
    for event_type in (
        "run.queued",
        "user.message.created",
        "tool.call.started",
        "assistant.message.completed",
        "user.message.created",
    ):
        await repository.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type=event_type,
                actor_type="contract",
            )
        )

    events = await repository.list_conversation_after(SESSION_ID, 1, principal(), limit=2)

    assert [(event.sequence, event.event_type) for event in events] == [
        (2, "user.message.created"),
        (4, "assistant.message.completed"),
    ]
    with pytest.raises(ValueError, match="nonnegative"):
        await repository.list_conversation_after(SESSION_ID, 0, principal(), limit=-1)


async def test_event_repository_omits_scheduled_seed_before_applying_the_page_limit() -> None:
    _clock, _sessions, _runs, repository = await memory_stack()
    for event_type, actor_type in (
        ("user.message.created", "scheduler"),
        ("assistant.message.completed", "runtime"),
        ("user.message.created", "principal"),
    ):
        await repository.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type=event_type,
                actor_type=actor_type,
            )
        )

    first = await repository.list_conversation_after(SESSION_ID, 0, principal(), limit=1)
    second = await repository.list_conversation_after(
        SESSION_ID, first[-1].sequence, principal(), limit=1
    )

    assert [(event.sequence, event.event_type) for event in first + second] == [
        (2, "assistant.message.completed"),
        (3, "user.message.created"),
    ]
