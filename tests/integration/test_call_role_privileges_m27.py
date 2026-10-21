"""The call worker reconciles and expires calls under exactly its documented grants.

`docs/bland-setup.md` grants the `veetbot_call` login a fixed least-privilege
set. Every reconciliation pass starts with the thirty-day retention sweep, so a
grant that call erasure lacks blocks every call result. In production a
`SELECT ... FOR UPDATE` on `sessions` failed this way from 2026-10-11, when the
first call aged out, until 2026-10-20. This test drives the real
`build_call_worker` composition as a NOINHERIT, NOBYPASSRLS login holding only
the documented grants. It covers a call that expires with derived copies in a
conversation, and a fresh outbound result with its notification.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build, build_call_worker
from agent_core.config import load_call_worker_settings
from agent_core.domain.calls import CallRecord
from agent_core.domain.mcp import MCPCallResult
from tests.contract.support import NOW, principal
from tests.contract.test_bland_client_contract import CALL_ID, KEY, RECIPIENT
from tests.contract.test_call_source_erasure_contract import assert_call_source_erasure
from tests.gates.test_call_lifecycle import normalized_call
from tests.gates.test_call_m27 import _shipped_role_environment, call_configuration
from tests.integration.m2_support import database_settings
from tests.integration.role_probe import release_probe_role

ROOT = Path(__file__).resolve().parents[2]
LATER = NOW + timedelta(days=31)
FRESH_CALL_ID = "00000000-0000-0000-0000-000000000028"

# The `veetbot_call` row of docs/bland-setup.md, with notifications enabled.
CALL_WORKER_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "alembic_version": frozenset({"SELECT"}),
    "sessions": frozenset({"SELECT", "UPDATE"}),
    "memories": frozenset({"SELECT"}),
    "memory_revisions": frozenset({"SELECT", "DELETE"}),
    "call_records": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "events": frozenset({"SELECT", "UPDATE"}),
    "tool_invocations": frozenset({"SELECT", "UPDATE"}),
    "runs": frozenset({"SELECT", "UPDATE"}),
    "session_history_items": frozenset({"SELECT", "UPDATE"}),
    "recall_traces": frozenset({"SELECT", "UPDATE"}),
    "artifacts": frozenset({"SELECT", "UPDATE"}),
    "checkpoints": frozenset({"SELECT", "DELETE"}),
    "integrated_episodes": frozenset({"SELECT", "DELETE"}),
    "knowledge_documents": frozenset({"SELECT", "DELETE"}),
    "notification_outbox": frozenset({"SELECT", "INSERT"}),
}


def documented_call_worker_privileges() -> dict[str, frozenset[str]]:
    """Parse the setup guide's `veetbot_call` grant row into table privileges."""

    row = next(
        line
        for line in (ROOT / "docs" / "bland-setup.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `veetbot_call` |")
    )
    granted: dict[str, set[str]] = {}
    for clause in row.split("|")[2].split(";"):
        match = re.fullmatch(r"\s*`([A-Z, ]+)` on (.+?)(?: when .*)?\s*", clause)
        if match is None:
            assert clause.strip() == "Schema usage", clause
            continue
        for table in re.findall(r"`([a-z_]+)`", match.group(2)):
            granted.setdefault(table, set()).update(match.group(1).split(", "))
    return {table: frozenset(privileges) for table, privileges in granted.items()}


def test_setup_guide_documents_the_tested_call_worker_grants() -> None:
    assert documented_call_worker_privileges() == CALL_WORKER_TABLE_PRIVILEGES


async def test_call_worker_expires_and_records_calls_under_documented_grants(
    tmp_path: Path,
) -> None:
    owner = principal()
    dispatch_key = str(uuid4())
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:

        async def put(kind: str, key: str, payload: dict[str, Any]) -> None:
            async with app.uow_factory() as uow:
                await uow.calls.put(
                    CallRecord(
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        kind=kind,
                        key=key,
                        revision=1,
                        payload=payload,
                        created_at=LATER,
                        updated_at=LATER,
                    ),
                    expected_revision=0,
                )

        # A retained call now past its thirty days, and an outbound call that
        # was dispatched moments ago and still waits for its result.
        await put(
            "call",
            CALL_ID,
            {**normalized_call(), "call_id": CALL_ID, "source": "bland", "account_id": "primary"},
        )
        await put(
            "dispatch",
            dispatch_key,
            {
                "active": True,
                "status": "accepted",
                "provider_call_id": FRESH_CALL_ID,
                "account_id": "primary",
                "recipient_hash": hashlib.sha256(RECIPIENT.encode()).hexdigest(),
                "run_id": str(uuid4()),
                "session_id": str(uuid4()),
            },
        )
        await put("receipt", FRESH_CALL_ID, {"active": True, "status": "pending"})

        async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
            if name == "provider_list_calls":
                return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
            assert args == {"provider_call_id": FRESH_CALL_ID}
            return MCPCallResult(
                structured={
                    **normalized_call(),
                    "provider_call_id": FRESH_CALL_ID,
                    "request_id": dispatch_key,
                    "direction": "outbound",
                    "created_at": LATER.isoformat(),
                }
            )

        async with release_probe_role("veetbot_call_probe", CALL_WORKER_TABLE_PRIVILEGES) as url:
            configuration = tmp_path / "calls.json"
            configuration.write_text(call_configuration().model_dump_json())
            credential = tmp_path / "bland-api-key"
            credential.write_text(KEY)
            credential.chmod(0o600)
            shipped = load_call_worker_settings(
                {
                    **_shipped_role_environment("veetbot-call.env.example"),
                    "AUTH_TENANT_ID": owner.tenant_id,
                    "AUTH_PRINCIPAL_ID": owner.principal_id,
                    "AGENT_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
                    "AGENT_CALL_ENABLED": "1",
                    "AGENT_CALL_NOTIFICATIONS_ENABLED": "1",
                    "AGENT_NOTIFICATION_API_ENABLED": "1",
                    "AGENT_NOTIFICATION_DISPATCH_ENABLED": "1",
                    "BLAND_CONFIGURATION_FILE": str(configuration),
                    "BLAND_API_KEY_FILE": str(credential),
                }
            )
            # The shipped login placeholder gives way to the probe login.
            settings = replace(shipped, database_url=url)
            async with build_call_worker(settings=settings, clock=FixedClock(LATER)) as calling:
                assert calling.worker is not None
                service = calling.worker.service
                service.provider = provider

                async def reconcile() -> None:
                    assert await service.reconcile() == 1

                await assert_call_source_erasure(app.uow_factory, reconcile)

        async with app.uow_factory() as uow:
            expired = await uow.calls.get(owner, "call", CALL_ID)
            fresh = await uow.calls.get(owner, "call", FRESH_CALL_ID)
            notifications = await uow.notification_outbox.list(owner, limit=10)
        assert expired is not None and expired.payload["erased"] is True
        assert fresh is not None and fresh.payload["status"] == "completed"
        assert len(notifications) == 1
