"""Regression coverage for the production scheduler database role."""

from scripts.check_schedule_database_permissions import (
    REQUIRED_TABLE_PRIVILEGES,
    ScheduleDatabaseRole,
    permission_failures,
)


def _role(
    *,
    server_version_num: int = 160000,
    superuser: bool = False,
    createdb: bool = False,
    createrole: bool = False,
    inherit: bool = False,
    replication: bool = False,
    bypass_rls: bool = False,
    settable_roles: frozenset[str] = frozenset(),
    table_privileges: dict[str, frozenset[str]] | None = None,
    column_privileges: dict[str, frozenset[str]] | None = None,
) -> ScheduleDatabaseRole:
    return ScheduleDatabaseRole(
        name="veetbot_schedule",
        server_version_num=server_version_num,
        superuser=superuser,
        createdb=createdb,
        createrole=createrole,
        inherit=inherit,
        replication=replication,
        bypass_rls=bypass_rls,
        settable_roles=settable_roles,
        table_privileges=(
            dict(REQUIRED_TABLE_PRIVILEGES) if table_privileges is None else table_privileges
        ),
        column_privileges={} if column_privileges is None else column_privileges,
    )


def test_schedule_role_rejects_unsupported_postgresql_before_privilege_checks() -> None:
    assert permission_failures(
        _role(
            server_version_num=170000,
            superuser=True,
            table_privileges={},
        )
    ) == [
        "schedule database role 'veetbot_schedule' requires PostgreSQL 16; "
        "connected server_version_num is 170000"
    ]


def test_schedule_role_requires_checkpoint_projection_privileges() -> None:
    granted = dict(REQUIRED_TABLE_PRIVILEGES)
    granted["projection_watermarks"] = frozenset({"INSERT", "UPDATE"})
    granted["session_history_items"] = frozenset({"INSERT"})

    assert permission_failures(_role(table_privileges=granted)) == [
        "schedule database role 'veetbot_schedule' lacks DELETE on public.projection_watermarks",
        "schedule database role 'veetbot_schedule' lacks SELECT on public.projection_watermarks",
        "schedule database role 'veetbot_schedule' lacks DELETE on public.session_history_items",
        "schedule database role 'veetbot_schedule' lacks SELECT on public.session_history_items",
    ]


def test_schedule_role_rejects_administrative_authority() -> None:
    assert permission_failures(
        _role(
            superuser=True,
            createdb=True,
            createrole=True,
            inherit=True,
            replication=True,
            bypass_rls=True,
        )
    )[:6] == [
        "schedule database role 'veetbot_schedule' must not be a superuser",
        "schedule database role 'veetbot_schedule' must not have CREATEDB",
        "schedule database role 'veetbot_schedule' must not have CREATEROLE",
        "schedule database role 'veetbot_schedule' must have NOINHERIT",
        "schedule database role 'veetbot_schedule' must not have REPLICATION",
        "schedule database role 'veetbot_schedule' must not have BYPASSRLS",
    ]


def test_schedule_role_rejects_every_surplus_effective_table_privilege() -> None:
    granted = dict(REQUIRED_TABLE_PRIVILEGES)
    granted["events"] = granted["events"] | {"DELETE"}
    granted["unrelated_table"] = frozenset({"SELECT"})

    assert permission_failures(_role(table_privileges=granted)) == [
        "schedule database role 'veetbot_schedule' has unexpected DELETE on public.events",
        (
            "schedule database role 'veetbot_schedule' has unexpected SELECT "
            "on public.unrelated_table"
        ),
    ]


def test_schedule_role_rejects_settable_membership_despite_noinherit() -> None:
    assert permission_failures(
        _role(inherit=False, settable_roles=frozenset({"database_admin"}))
    ) == [
        "schedule database role 'veetbot_schedule' can SET ROLE to unexpected role 'database_admin'"
    ]


def test_schedule_role_rejects_column_privilege_that_bypasses_table_allowlist() -> None:
    assert permission_failures(
        _role(column_privileges={"unrelated_table": frozenset({"SELECT"})})
    ) == [
        (
            "schedule database role 'veetbot_schedule' has unexpected column-level SELECT "
            "on public.unrelated_table"
        )
    ]


def test_schedule_role_accepts_complete_least_privilege_inventory() -> None:
    assert permission_failures(_role()) == []
