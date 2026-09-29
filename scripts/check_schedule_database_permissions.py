"""Fail unless the production schedule database role can materialize runs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from scripts.database_role_permissions import (
    DatabaseRole,
    check_database_role,
    inspect_database_role,
)
from scripts.database_role_permissions import (
    permission_failures as role_permission_failures,
)

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

ScheduleDatabaseRole = DatabaseRole
inspect_schedule_database_role = inspect_database_role


def permission_failures(role: ScheduleDatabaseRole) -> list[str]:
    return role_permission_failures(role, label="schedule", required=REQUIRED_TABLE_PRIVILEGES)


def main() -> int:
    return asyncio.run(check_database_role(label="schedule", required=REQUIRED_TABLE_PRIVILEGES))


if __name__ == "__main__":
    raise SystemExit(main())
