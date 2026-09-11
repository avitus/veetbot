"""Real PostgreSQL execution of the source erasure contract."""

import pytest

from agent_core.bootstrap import build
from tests.contract.support import principal
from tests.contract.test_email_source_erasure_contract import (
    assert_email_source_erasure,
    assert_erasure_waits_for_potential_producer,
)
from tests.integration.m2_support import database_settings


async def test_postgres_source_erasure_contract() -> None:
    async with build(
        settings=database_settings(), storage="postgres", principal=principal()
    ) as composition:
        await assert_email_source_erasure(composition.uow_factory)


@pytest.mark.parametrize("kind", ["bound", "chat_scope", "gmail_scope", "invocation"])
async def test_postgres_erasure_waits_for_inflight_source_producer(kind: str) -> None:
    async with build(
        settings=database_settings(), storage="postgres", principal=principal()
    ) as composition:
        await assert_erasure_waits_for_potential_producer(composition.uow_factory, kind)
