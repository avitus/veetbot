"""PostgreSQL parity for bounded, run-scoped completion lookup."""

from uuid import UUID

from agent_core.bootstrap import build
from agent_core.domain.runs import RunStatus
from tests.contract.support import principal, run, session
from tests.contract.test_event_repository_contract import (
    assert_latest_event_filters_run_before_selection,
)
from tests.integration.m2_support import database_settings


async def test_postgres_latest_event_filters_run_before_selection() -> None:
    """Execute the shared completion query contract against actual SQL persistence."""
    async with (
        build(settings=database_settings(), storage="postgres", principal=principal()) as app,
        app.uow_factory() as uow,
    ):
        await uow.sessions.create(session())
        for identifier in (701, 702):
            await uow.runs.create(
                run(status=RunStatus.COMPLETED).model_copy(update={"id": UUID(int=identifier)})
            )
        await assert_latest_event_filters_run_before_selection(uow.events)
