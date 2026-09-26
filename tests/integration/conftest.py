from __future__ import annotations

import pytest

from tests.integration.disposable_database import truncate_application_tables


@pytest.fixture(autouse=True)
async def isolate_postgres_case(request: pytest.FixtureRequest) -> None:
    """Remove prior synthetic records, including before migration round trips.

    The reset skips the case unless the run marked its database disposable.
    Migration cases also run against an empty or older schema, so they clear
    only application tables that already exist, preserving alembic_version and
    the production data-loss refusal.
    """

    await truncate_application_tables(existing_only=request.path.name == "test_migrations.py")
