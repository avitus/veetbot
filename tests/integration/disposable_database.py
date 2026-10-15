"""The one way the integration suite reaches and resets its database.

Every integration case truncates the application tables, and CI's database URL
is the same as the shared local development database's, so nothing about the URL
can tell a scratch database from one that holds someone's work. A run erases a
database only after marking it disposable with ``VEETBOT_TEST_DATABASE_DISPOSABLE=1``.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

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


@asynccontextmanager
async def paired_recall_databases() -> AsyncIterator[tuple[str, str]]:
    """Create two opted-in empty clones before admitting either scenario worker."""
    async with clone_recall_database() as first:
        # The original template has the first clone's guard connection open.
        # Copy the owned, still-empty clone before either worker opens it.
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("DATABASE_URL", first)
            async with clone_recall_database() as second:
                yield first, second


@asynccontextmanager
async def clone_recall_database() -> AsyncIterator[str]:
    """Clone only an opted-in scratch schema into a new, uniquely named eval DB.

    Only the database successfully created by this invocation is removed. The
    template URL and all cleanup remain behind the integration opt-in guard.
    The creation epoch supports operator recovery after a killed process; age
    alone never authorizes this helper to destroy another invocation's database.
    """
    database_url = disposable_database_url()
    source = make_url(database_url)
    # Keep the suffix inside the evaluator's existing 32-character guard.
    name = f"veetbot_m33_eval_{int(time.time())}_{uuid4().hex[:20]}"
    engine = create_engine(database_url)
    quoted_source = engine.dialect.identifier_preparer.quote_identifier(source.database or "")
    try:
        async with engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE {quoted_source}'))
            try:
                yield source.set(database=name).render_as_string(hide_password=False)
            finally:
                await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    finally:
        await engine.dispose()
