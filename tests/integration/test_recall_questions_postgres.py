"""PostgreSQL recall-question store: the shared contract and forced row security."""

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.recall_questions import PostgresRecallQuestionStore
from tests.contract.test_recall_question_store_contract import CONTRACTS
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda fn: fn.__name__)
async def test_postgres_recall_question_store_contract(contract) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
            )
            await contract(PostgresRecallQuestionStore(session))
    finally:
        await engine.dispose()


async def test_recall_question_tables_force_row_level_security() -> None:
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            flags = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('recall_questions','recall_question_revisions') "
                        "ORDER BY relname"
                    )
                )
            ).all()
            assert [tuple(row) for row in flags] == [
                ("recall_question_revisions", True, True),
                ("recall_questions", True, True),
            ]
    finally:
        await engine.dispose()
