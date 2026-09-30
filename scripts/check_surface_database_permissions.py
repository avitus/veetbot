"""Fail unless the production surface database role holds exactly its allowlist."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from scripts.database_role_permissions import (
    DatabaseRole,
    check_database_role,
)
from scripts.database_role_permissions import (
    permission_failures as role_permission_failures,
)

# Derived by running every surface path under a login holding only these, then
# removing each in turn (tests/integration/test_surface_role_privileges_m14.py).
# Several UPDATE grants exist only for SELECT ... FOR UPDATE row locks: on
# events and runs they serialize history and run writes with People erasure,
# and on approvals, surface_pairing_codes, surface_replies, notification_outbox
# and tool_invocations they take the row being claimed or resolved.
REQUIRED_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = {
    "agents": frozenset({"SELECT"}),
    "alembic_version": frozenset({"SELECT"}),
    "approvals": frozenset({"SELECT", "UPDATE"}),
    "checkpoints": frozenset({"INSERT", "SELECT"}),
    "delegations": frozenset({"SELECT", "UPDATE"}),
    "derived_event_keys": frozenset({"INSERT", "SELECT"}),
    "devices": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "events": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "export_consent": frozenset({"SELECT"}),
    "notification_deliveries": frozenset({"INSERT", "SELECT"}),
    "notification_outbox": frozenset({"SELECT", "UPDATE"}),
    "notification_run_receipts": frozenset({"SELECT"}),
    "process_events": frozenset({"INSERT", "SELECT"}),
    "projection_watermarks": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "runs": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "session_history_items": frozenset({"INSERT", "SELECT"}),
    "sessions": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "surface_inbound_receipts": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "surface_pairing_codes": frozenset({"SELECT", "UPDATE"}),
    "surface_pairings": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "surface_replies": frozenset({"SELECT", "UPDATE"}),
    "surface_sender_lockouts": frozenset({"DELETE", "INSERT", "SELECT", "UPDATE"}),
    "surface_sessions": frozenset({"INSERT", "SELECT", "UPDATE"}),
    "tool_invocations": frozenset({"SELECT", "UPDATE"}),
}


def permission_failures(role: DatabaseRole) -> list[str]:
    return role_permission_failures(role, label="surface", required=REQUIRED_TABLE_PRIVILEGES)


def main() -> int:
    return asyncio.run(check_database_role(label="surface", required=REQUIRED_TABLE_PRIVILEGES))


if __name__ == "__main__":
    raise SystemExit(main())
