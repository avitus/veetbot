"""Exact least-privilege validation shared by the restricted database logins."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Production is pinned to PostgreSQL 16. PostgreSQL 17's MAINTAIN privilege is
# intentionally absent until the deployment version changes.
POSTGRESQL_16_TABLE_PRIVILEGES = (
    "DELETE",
    "INSERT",
    "REFERENCES",
    "SELECT",
    "TRIGGER",
    "TRUNCATE",
    "UPDATE",
)
COLUMN_PRIVILEGES = ("INSERT", "REFERENCES", "SELECT", "UPDATE")


@dataclass(frozen=True)
class DatabaseRole:
    name: str
    server_version_num: int
    superuser: bool
    createdb: bool
    createrole: bool
    inherit: bool
    replication: bool
    bypass_rls: bool
    settable_roles: frozenset[str]
    table_privileges: Mapping[str, frozenset[str]]
    column_privileges: Mapping[str, frozenset[str]]


def permission_failures(
    role: DatabaseRole,
    *,
    label: str,
    required: Mapping[str, frozenset[str]],
) -> list[str]:
    """Compare a login with its exact allowlist; ``label`` names the role in messages."""

    subject = f"{label} database role {role.name!r}"
    if not 160000 <= role.server_version_num < 170000:
        return [
            f"{subject} requires PostgreSQL 16; "
            f"connected server_version_num is {role.server_version_num}"
        ]
    failures: list[str] = []
    if role.superuser:
        failures.append(f"{subject} must not be a superuser")
    if role.createdb:
        failures.append(f"{subject} must not have CREATEDB")
    if role.createrole:
        failures.append(f"{subject} must not have CREATEROLE")
    if role.inherit:
        failures.append(f"{subject} must have NOINHERIT")
    if role.replication:
        failures.append(f"{subject} must not have REPLICATION")
    if role.bypass_rls:
        failures.append(f"{subject} must not have BYPASSRLS")
    for settable_role in sorted(role.settable_roles):
        failures.append(f"{subject} can SET ROLE to unexpected role {settable_role!r}")
    for table_name, granted in sorted(role.table_privileges.items()):
        allowed = required.get(table_name, frozenset())
        for privilege in sorted(granted - allowed):
            failures.append(f"{subject} has unexpected {privilege} on public.{table_name}")
    for table_name, granted in sorted(role.column_privileges.items()):
        for privilege in sorted(granted):
            failures.append(
                f"{subject} has unexpected column-level {privilege} on public.{table_name}"
            )
    for table_name, needed in sorted(required.items()):
        granted = role.table_privileges.get(table_name, frozenset())
        for privilege in sorted(needed - granted):
            failures.append(f"{subject} lacks {privilege} on public.{table_name}")
    return failures


async def inspect_database_role(database_url: str) -> DatabaseRole:
    """Read the connected login's attributes and every effective public privilege.

    Tables, views, materialized views and foreign tables are all probed, so a
    grant on any readable relation counts against the allowlist.
    """

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT current_user AS name, "
                        "current_setting('server_version_num')::integer "
                        "AS server_version_num, rolsuper, rolcreatedb, "
                        "rolcreaterole, rolinherit, rolreplication, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
            server_version_num = int(row.server_version_num)
            if not 160000 <= server_version_num < 170000:
                return DatabaseRole(
                    name=str(row.name),
                    server_version_num=server_version_num,
                    superuser=bool(row.rolsuper),
                    createdb=bool(row.rolcreatedb),
                    createrole=bool(row.rolcreaterole),
                    inherit=bool(row.rolinherit),
                    replication=bool(row.rolreplication),
                    bypass_rls=bool(row.rolbypassrls),
                    settable_roles=frozenset(),
                    table_privileges={},
                    column_privileges={},
                )
            settable_role_rows = (
                await connection.execute(
                    text(
                        "SELECT role.rolname AS role_name "
                        "FROM pg_roles AS role "
                        "WHERE role.rolname <> current_user "
                        "AND pg_has_role(current_user, role.oid, 'SET') "
                        "ORDER BY role.rolname"
                    )
                )
            ).all()
            privilege_rows = (
                await connection.execute(
                    text(
                        "SELECT tables.relname AS table_name, privileges.privilege "
                        "FROM pg_class AS tables "
                        "JOIN pg_namespace AS schemas ON schemas.oid = tables.relnamespace "
                        "CROSS JOIN unnest(ARRAY["
                        + ", ".join(
                            f"'{privilege}'" for privilege in POSTGRESQL_16_TABLE_PRIVILEGES
                        )
                        + "]) AS privileges(privilege) "
                        "WHERE schemas.nspname = 'public' "
                        "AND tables.relkind IN ('r', 'p', 'v', 'm', 'f') "
                        "AND has_table_privilege(current_user, tables.oid, privileges.privilege) "
                        "ORDER BY tables.relname, privileges.privilege"
                    )
                )
            ).all()
            column_privilege_rows = (
                await connection.execute(
                    text(
                        "SELECT tables.relname AS table_name, privileges.privilege "
                        "FROM pg_class AS tables "
                        "JOIN pg_namespace AS schemas ON schemas.oid = tables.relnamespace "
                        "CROSS JOIN unnest(ARRAY["
                        + ", ".join(f"'{privilege}'" for privilege in COLUMN_PRIVILEGES)
                        + "]) AS privileges(privilege) "
                        "WHERE schemas.nspname = 'public' "
                        "AND tables.relkind IN ('r', 'p', 'v', 'm', 'f') "
                        "AND has_any_column_privilege("
                        "current_user, tables.oid, privileges.privilege"
                        ") "
                        "AND NOT has_table_privilege("
                        "current_user, tables.oid, privileges.privilege"
                        ") "
                        "ORDER BY tables.relname, privileges.privilege"
                    )
                )
            ).all()
            granted_sets: dict[str, set[str]] = {}
            for privilege_row in privilege_rows:
                granted_sets.setdefault(str(privilege_row.table_name), set()).add(
                    str(privilege_row.privilege)
                )
            column_granted_sets: dict[str, set[str]] = {}
            for privilege_row in column_privilege_rows:
                column_granted_sets.setdefault(str(privilege_row.table_name), set()).add(
                    str(privilege_row.privilege)
                )
            return DatabaseRole(
                name=str(row.name),
                server_version_num=server_version_num,
                superuser=bool(row.rolsuper),
                createdb=bool(row.rolcreatedb),
                createrole=bool(row.rolcreaterole),
                inherit=bool(row.rolinherit),
                replication=bool(row.rolreplication),
                bypass_rls=bool(row.rolbypassrls),
                settable_roles=frozenset(
                    str(settable_role_row.role_name) for settable_role_row in settable_role_rows
                ),
                table_privileges={
                    table_name: frozenset(privileges)
                    for table_name, privileges in granted_sets.items()
                },
                column_privileges={
                    table_name: frozenset(privileges)
                    for table_name, privileges in column_granted_sets.items()
                },
            )
    finally:
        await engine.dispose()


async def check_database_role(*, label: str, required: Mapping[str, frozenset[str]]) -> int:
    """Validate the login in ``DATABASE_URL`` and report each failure on stderr."""

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print(f"FAIL: {label} worker DATABASE_URL is required", file=sys.stderr)
        return 1
    try:
        role = await inspect_database_role(database_url)
    except (OSError, SQLAlchemyError):
        print(
            f"FAIL: {label} database role permission probe could not connect or query",
            file=sys.stderr,
        )
        return 1
    failures = permission_failures(role, label=label, required=required)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"OK: {label} database role {role.name!r} has the required least privileges")
    return 0
