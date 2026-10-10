"""PostgreSQL parity for background-maintenance conversation recency."""

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from tests.contract.support import NOW, principal, session
from tests.contract.test_session_activity import (
    assert_memory_maintenance_preserves_activity,
    assert_missing_session_leaves_no_event,
)
from tests.integration.m2_support import database_settings


async def test_postgres_memory_maintenance_preserves_activity() -> None:
    clock = FixedClock(NOW)
    async with (
        build(
            settings=database_settings(), storage="postgres", principal=principal(), clock=clock
        ) as app,
        app.uow_factory() as uow,
    ):
        await uow.sessions.create(session())
        await assert_memory_maintenance_preserves_activity(clock, uow.sessions, uow.events)


@pytest.mark.parametrize(
    ("event_type", "actor_type"),
    [
        ("memory.decayed", "memory"),
        ("memory.retired", "memory"),
        ("user.message.created", "principal"),
    ],
)
async def test_postgres_missing_session_append_is_atomic(event_type: str, actor_type: str) -> None:
    async with (
        build(settings=database_settings(), storage="postgres", principal=principal()) as app,
        app.uow_factory() as uow,
    ):
        await assert_missing_session_leaves_no_event(
            uow.sessions, uow.events, event_type, actor_type
        )
