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


@dataclass(frozen=True)
class ScheduleDatabaseRole:
    name: str
    superuser: bool
    bypass_rls: bool
    table_privileges: Mapping[str, frozenset[str]]


def permission_failures(role: ScheduleDatabaseRole) -> list[str]:
    failures: list[str] = []
    if role.superuser:
        failures.append(f"schedule database role {role.name!r} must not be a superuser")
    if role.bypass_rls:
        failures.append(f"schedule database role {role.name!r} must not have BYPASSRLS")
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
                        "SELECT current_user AS name, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
            granted: dict[str, frozenset[str]] = {}
            for table_name, required in REQUIRED_TABLE_PRIVILEGES.items():
                table_grants: set[str] = set()
                for privilege in required:
                    allowed = await connection.scalar(
                        text("SELECT has_table_privilege(current_user, :relation, :privilege)"),
                        {
                            "relation": f"public.{table_name}",
                            "privilege": privilege,
                        },
                    )
                    if allowed:
                        table_grants.add(privilege)
                granted[table_name] = frozenset(table_grants)
            return ScheduleDatabaseRole(
                name=str(row.name),
                superuser=bool(row.rolsuper),
                bypass_rls=bool(row.rolbypassrls),
                table_privileges=granted,
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
