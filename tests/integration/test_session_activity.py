"""PostgreSQL parity for background-maintenance conversation recency."""

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from tests.contract.support import NOW, principal, session
from tests.contract.test_session_activity import assert_memory_maintenance_preserves_activity
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
