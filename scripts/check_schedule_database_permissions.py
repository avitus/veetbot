"""Fail unless the production schedule database role can materialize runs."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

REQUIRED_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = {
    "agents": frozenset({"SELECT"}),
    "alembic_version": frozenset({"SELECT"}),
    "checkpoints": frozenset({"DELETE", "INSERT", "SELECT"}),
    "derived_event_keys": frozenset({"INSERT", "SELECT"}),
    "events": frozenset({"INSERT", "SELECT"}),
    "notification_outbox": frozenset({"INSERT", "SELECT"}),
    "process_events": frozenset({"INSERT", "SELECT"}),
    "projection_watermarks": frozenset({"DELETE", "INSERT", "SELECT", "UPDATE"}),
    "runs": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "schedule_occurrences": frozenset({"INSERT", "SELECT"}),
    "schedule_revisions": frozenset({"SELECT"}),
    "schedules": frozenset({"SELECT", "UPDATE"}),
    "session_history_items": frozenset({"DELETE", "INSERT", "SELECT"}),
    "sessions": frozenset({"INSERT", "SELECT", "UPDATE"}),
}
TABLE_PRIVILEGES = (
    "DELETE",
    "INSERT",
    "REFERENCES",
    "SELECT",
    "TRIGGER",
    "TRUNCATE",
    "UPDATE",
)


@dataclass(frozen=True)
class ScheduleDatabaseRole:
    name: str
    superuser: bool
    createdb: bool
    createrole: bool
    inherit: bool
    replication: bool
    bypass_rls: bool
    table_privileges: Mapping[str, frozenset[str]]


def permission_failures(role: ScheduleDatabaseRole) -> list[str]:
    failures: list[str] = []
    if role.superuser:
        failures.append(f"schedule database role {role.name!r} must not be a superuser")
    if role.createdb:
        failures.append(f"schedule database role {role.name!r} must not have CREATEDB")
    if role.createrole:
        failures.append(f"schedule database role {role.name!r} must not have CREATEROLE")
    if role.inherit:
        failures.append(f"schedule database role {role.name!r} must have NOINHERIT")
    if role.replication:
        failures.append(f"schedule database role {role.name!r} must not have REPLICATION")
    if role.bypass_rls:
        failures.append(f"schedule database role {role.name!r} must not have BYPASSRLS")
    for table_name, granted in sorted(role.table_privileges.items()):
        allowed = REQUIRED_TABLE_PRIVILEGES.get(table_name, frozenset())
        for privilege in sorted(granted - allowed):
            failures.append(
                f"schedule database role {role.name!r} has unexpected {privilege} "
                f"on public.{table_name}"
            )
    for table_name, required in sorted(REQUIRED_TABLE_PRIVILEGES.items()):
        granted = role.table_privileges.get(table_name, frozenset())
        for privilege in sorted(required - granted):
            failures.append(
                f"schedule database role {role.name!r} lacks {privilege} on public.{table_name}"
            )
    return failures


async def inspect_schedule_database_role(database_url: str) -> ScheduleDatabaseRole:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT current_user AS name, rolsuper, rolcreatedb, "
                        "rolcreaterole, rolinherit, rolreplication, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
            privilege_rows = (
                await connection.execute(
                    text(
                        "SELECT tables.relname AS table_name, privileges.privilege "
                        "FROM pg_class AS tables "
                        "JOIN pg_namespace AS schemas ON schemas.oid = tables.relnamespace "
                        "CROSS JOIN unnest(ARRAY["
                        + ", ".join(f"'{privilege}'" for privilege in TABLE_PRIVILEGES)
                        + "]) AS privileges(privilege) "
                        "WHERE schemas.nspname = 'public' "
                        "AND tables.relkind IN ('r', 'p') "
                        "AND has_table_privilege(current_user, tables.oid, privileges.privilege) "
                        "ORDER BY tables.relname, privileges.privilege"
                    )
                )
            ).all()
            granted_sets: dict[str, set[str]] = {}
            for privilege_row in privilege_rows:
                granted_sets.setdefault(str(privilege_row.table_name), set()).add(
                    str(privilege_row.privilege)
                )
            return ScheduleDatabaseRole(
                name=str(row.name),
                superuser=bool(row.rolsuper),
                createdb=bool(row.rolcreatedb),
                createrole=bool(row.rolcreaterole),
                inherit=bool(row.rolinherit),
                replication=bool(row.rolreplication),
                bypass_rls=bool(row.rolbypassrls),
                table_privileges={
                    table_name: frozenset(privileges)
                    for table_name, privileges in granted_sets.items()
                },
            )
    finally:
        await engine.dispose()


async def _run() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("FAIL: schedule worker DATABASE_URL is required", file=sys.stderr)
        return 1
    try:
        role = await inspect_schedule_database_role(database_url)
    except (OSError, SQLAlchemyError):
        print(
            "FAIL: schedule database role permission probe could not connect or query",
            file=sys.stderr,
        )
        return 1
    failures = permission_failures(role)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"OK: schedule database role {role.name!r} has the required least privileges")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
