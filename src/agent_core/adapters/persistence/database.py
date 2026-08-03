"""Async engine construction and pinned-schema refusal."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_core.adapters.persistence.revision import EXPECTED_REVISION
from agent_core.domain.errors import ConflictError


class SchemaRevisionError(ConflictError):
    """The database revision does not match the code's pinned revision."""


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def assert_schema_revision(engine: AsyncEngine) -> None:
    try:
        async with engine.connect() as connection:
            actual = list(
                (
                    await connection.execute(
                        text("SELECT version_num FROM alembic_version ORDER BY version_num")
                    )
                ).scalars()
            )
    except ProgrammingError as exc:
        raise SchemaRevisionError(
            f"database revision is absent; expected {EXPECTED_REVISION}; run migrations first"
        ) from exc
    if not actual:
        raise SchemaRevisionError(
            f"database revision is absent; expected {EXPECTED_REVISION}; run migrations first"
        )
    if actual != [EXPECTED_REVISION]:
        label = "revision" if len(actual) == 1 else "revisions"
        raise SchemaRevisionError(
            f"database {label} {', '.join(actual)} does not match expected "
            f"{EXPECTED_REVISION}; "
            "startup will not migrate or serve"
        )
