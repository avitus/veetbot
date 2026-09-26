"""The one way the integration suite reaches and resets its database.

Every integration case truncates the application tables, and CI's database URL
is the same as the shared local development database's, so nothing about the URL
can tell a scratch database from one that holds someone's work. A run erases a
database only after marking it disposable with ``VEETBOT_TEST_DATABASE_DISPOSABLE=1``.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text

from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import Base

DISPOSABLE_FLAG = "VEETBOT_TEST_DATABASE_DISPOSABLE"
NOT_DISPOSABLE = (
    f"DATABASE_URL is set but {DISPOSABLE_FLAG} is not 1: integration tests erase "
    "every application table, so they run only against a scratch database. Create "
    "one, point DATABASE_URL at it, run `make migrate`, and rerun with "
    f"{DISPOSABLE_FLAG}=1 (docs/plan/development-toolchain.md)."
)


def disposable_database_url() -> str:
    """Return ``DATABASE_URL`` only when this run has marked it disposable."""

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    if os.environ.get(DISPOSABLE_FLAG) != "1":
        pytest.skip(NOT_DISPOSABLE)
    return database_url


async def truncate_application_tables(*, existing_only: bool = False) -> None:
    """Empty the application tables, keeping ``alembic_version``.

    ``existing_only`` clears only tables already present, for migration cases
    that also run against an empty or older schema.
    """

    engine = create_engine(disposable_database_url())
    try:
        async with engine.begin() as connection:
            tables = Base.metadata.sorted_tables
            if existing_only:
                existing = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
                tables = [table for table in tables if table.name in existing]
            if tables:
                table_names = ", ".join(f'"{table.name}"' for table in tables)
                await connection.execute(
                    text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE")
                )
    finally:
        await engine.dispose()
