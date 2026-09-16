from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text

from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import Base


@pytest.fixture(autouse=True)
async def isolate_postgres_case(request: pytest.FixtureRequest) -> None:
    """Remove prior synthetic records, including before migration round trips."""

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            tables = Base.metadata.sorted_tables
            if request.path.name == "test_migrations.py":
                # Migration cases also run against an empty or older schema.
                # Clear only application tables that already exist, preserving
                # alembic_version and the production data-loss refusal.
                existing = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
                tables = [table for table in tables if table.name in existing]
            if tables:
                table_names = ", ".join(f'"{table.name}"' for table in tables)
                await connection.execute(
                    text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE")
                )
    finally:
        await engine.dispose()
