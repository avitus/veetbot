"""PostgreSQL parity for the Milestone 31 subscription records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.email import PostgresEmailStore
from tests.contract.support import principal
from tests.gates.test_email_unsubscribe_m31 import subscription_records_contract
from tests.integration.m2_support import database_settings


@asynccontextmanager
async def database() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_settings().database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_postgres_subscription_records_satisfy_the_shared_contract() -> None:
    """The same contract the in-memory store passes, under forced tenant RLS."""
    async with database() as engine, create_session_factory(engine)() as session:
        await session.execute(
            text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
            {"tenant": principal().tenant_id},
        )
        await subscription_records_contract(PostgresEmailStore(session), principal())
        await session.rollback()
