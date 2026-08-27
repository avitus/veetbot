"""Regression coverage for the production scheduler database role."""

from scripts.check_schedule_database_permissions import (
    REQUIRED_TABLE_PRIVILEGES,
    ScheduleDatabaseRole,
    permission_failures,
)


def _role(
    *,
    superuser: bool = False,
    createdb: bool = False,
    createrole: bool = False,
    inherit: bool = False,
    replication: bool = False,
    bypass_rls: bool = False,
    table_privileges: dict[str, frozenset[str]] | None = None,
) -> ScheduleDatabaseRole:
    return ScheduleDatabaseRole(
        name="veetbot_schedule",
        superuser=superuser,
        createdb=createdb,
        createrole=createrole,
        inherit=inherit,
        replication=replication,
        bypass_rls=bypass_rls,
        table_privileges=table_privileges or dict(REQUIRED_TABLE_PRIVILEGES),
    )


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


def test_schedule_role_accepts_complete_least_privilege_inventory() -> None:
    assert permission_failures(_role()) == []
