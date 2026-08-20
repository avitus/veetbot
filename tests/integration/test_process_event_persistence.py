"""PostgreSQL process-event repository contract."""

from agent_core.bootstrap import build
from tests.contract.test_process_event_repository_contract import (
    assert_process_event_repository_is_append_only_and_derivation_idempotent,
)
from tests.integration.m2_support import database_settings


async def test_postgres_process_event_repository_contract() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        composition.uow_factory() as uow,
    ):
        await assert_process_event_repository_is_append_only_and_derivation_idempotent(
            uow.process_events
        )
