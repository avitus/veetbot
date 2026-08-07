from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import Base


@pytest.fixture(autouse=True)
async def isolate_postgres_case(request: pytest.FixtureRequest) -> None:
    """Give each non-migration integration case an empty application schema."""

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    if request.path.name == "test_migrations.py":
        return
    engine = create_engine(database_url)
    table_names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()
