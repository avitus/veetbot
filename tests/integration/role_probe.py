"""Throwaway restricted PostgreSQL logins for least-privilege role tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url

from agent_core.adapters.persistence.database import create_engine
from tests.integration.m2_support import database_settings


@asynccontextmanager
async def release_probe_role(
    prefix: str, privileges: Mapping[str, frozenset[str]]
) -> AsyncIterator[str]:
    """Yield a NOINHERIT, NOBYPASSRLS login URL holding exactly ``privileges``."""

    admin_url = make_url(database_settings().database_url)
    role = f"{prefix}_{uuid4().hex[:12]}"
    password = uuid4().hex
    engine = create_engine(admin_url.render_as_string(hide_password=False))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            )
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            for table, table_privileges in privileges.items():
                if table_privileges:
                    await connection.execute(
                        text(f"GRANT {', '.join(sorted(table_privileges))} ON {table} TO {role}")
                    )
        try:
            yield admin_url.set(username=role, password=password).render_as_string(
                hide_password=False
            )
        finally:
            async with engine.begin() as connection:
                # A worker under test may still hold a pooled session in a failed case.
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE usename = :role"
                    ),
                    {"role": role},
                )
                await connection.execute(text(f"DROP OWNED BY {role}"))
                await connection.execute(text(f"DROP ROLE {role}"))
    finally:
        await engine.dispose()
