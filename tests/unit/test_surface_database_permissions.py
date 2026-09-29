"""Regression coverage for the production surface database role (Milestone 14)."""

import re
from pathlib import Path

from scripts.check_surface_database_permissions import (
    REQUIRED_TABLE_PRIVILEGES,
    permission_failures,
)
from scripts.database_role_permissions import DatabaseRole

ROOT = Path(__file__).resolve().parents[2]
ALL_TABLE_PRIVILEGES = frozenset(
    {"DELETE", "INSERT", "REFERENCES", "SELECT", "TRIGGER", "TRUNCATE", "UPDATE"}
)


def _role(
    *,
    name: str = "veetbot_surface",
    superuser: bool = False,
    inherit: bool = False,
    bypass_rls: bool = False,
    table_privileges: dict[str, frozenset[str]] | None = None,
) -> DatabaseRole:
    return DatabaseRole(
        name=name,
        server_version_num=160013,
        superuser=superuser,
        createdb=False,
        createrole=False,
        inherit=inherit,
        replication=False,
        bypass_rls=bypass_rls,
        settable_roles=frozenset(),
        table_privileges=(
            dict(REQUIRED_TABLE_PRIVILEGES) if table_privileges is None else table_privileges
        ),
        column_privileges={},
    )


def test_surface_role_accepts_exactly_its_least_privilege_inventory() -> None:
    assert permission_failures(_role()) == []


def test_surface_role_rejects_the_application_login() -> None:
    """The former configuration: the table-owning, superuser `agent` login."""

    failures = permission_failures(
        _role(
            name="agent",
            superuser=True,
            inherit=True,
            bypass_rls=True,
            table_privileges=dict.fromkeys(
                [*REQUIRED_TABLE_PRIVILEGES, "memories"], ALL_TABLE_PRIVILEGES
            ),
        )
    )

    assert failures[:3] == [
        "surface database role 'agent' must not be a superuser",
        "surface database role 'agent' must have NOINHERIT",
        "surface database role 'agent' must not have BYPASSRLS",
    ]
    assert "surface database role 'agent' has unexpected SELECT on public.memories" in failures
    assert "surface database role 'agent' has unexpected DELETE on public.events" in failures


def test_surface_role_requires_the_erasure_row_locks() -> None:
    granted = dict(REQUIRED_TABLE_PRIVILEGES)
    granted["events"] = frozenset({"INSERT", "SELECT"})
    granted["runs"] = frozenset({"INSERT", "SELECT"})

    assert permission_failures(_role(table_privileges=granted)) == [
        "surface database role 'veetbot_surface' lacks UPDATE on public.events",
        "surface database role 'veetbot_surface' lacks UPDATE on public.runs",
    ]


def test_surface_role_rejects_the_schedule_tables_it_never_touches() -> None:
    granted = dict(REQUIRED_TABLE_PRIVILEGES)
    granted["schedules"] = frozenset({"SELECT"})

    assert permission_failures(_role(table_privileges=granted)) == [
        "surface database role 'veetbot_surface' has unexpected SELECT on public.schedules"
    ]


def _documented_grants(document: str) -> dict[str, frozenset[str]]:
    granted: dict[str, set[str]] = {}
    for privilege, tables in re.findall(
        r"GRANT (SELECT|INSERT|UPDATE|DELETE) ON\s+([a-z_,\s]+?)\s+TO veetbot_surface;",
        document,
    ):
        for table in tables.replace(",", " ").split():
            granted.setdefault(table, set()).add(privilege)
    return {table: frozenset(privileges) for table, privileges in granted.items()}


def test_the_guide_and_the_runbook_grant_exactly_the_checked_allowlist() -> None:
    for document in ("docs/deployment.md", "docs/telegram-surface-runbook.md"):
        text = (ROOT / document).read_text(encoding="utf-8")
        assert _documented_grants(text) == dict(REQUIRED_TABLE_PRIVILEGES), document
        assert "NOINHERIT NOREPLICATION NOBYPASSRLS" in text, document
        assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM veetbot_surface" in text


def test_the_surface_environment_connects_as_the_dedicated_login() -> None:
    example = (ROOT / "deploy/veetbot-surface.env.example").read_text(encoding="utf-8")

    [database_url] = [line for line in example.splitlines() if line.startswith("DATABASE_URL=")]
    assert database_url.startswith("DATABASE_URL=postgresql+asyncpg://veetbot_surface:")
