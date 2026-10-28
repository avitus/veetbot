"""One outbound call end to end on PostgreSQL, through every documented boundary.

`docs/plan/bland-calling.md`, "Outbound tools and lifecycle" and "Inbound number
and result delivery": the signed public ingress enqueues a bounded call
identifier under the `veetbot_call_ingress` grants; the call worker reconciles
it under the `veetbot_call` grants through the real Bland adapter's status
normalization; the owner reads the result through `/v1/calls`; duplicate
callbacks produce one result and one notification; erasure leaves a content-free
tombstone a replay cannot resurrect.

Guards three production fixes: a dispatched call reads as pending, not
"call not found", until its result arrives (`c22678b3`); a voicemail-detection
hang-up records as `voicemail` (`17678d3b`); Bland's latest `status` keeps busy
and no-answer distinct from a completed conversation (`a33fb391`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.api import create_app
from agent_core.bootstrap import build, build_call_worker
from agent_core.config import Settings, load_call_worker_settings
from agent_core.domain.agents import Principal
from agent_core.domain.calls import CallRecord
from agent_core.domain.mcp import MCPCallResult
from agent_core.domain.notifications import NotificationKind
from bland_mcp.client import BlandClient
from tests.contract.support import NOW, principal
from tests.contract.test_bland_client_contract import (
    CALL_ID,
    KEY,
    NUMBER,
    RECIPIENT,
    provider_call,
    voicemail_detection_hangup,
)
from tests.gates.test_call_lifecycle import SIGNING_FIXTURE, signed_body
from tests.gates.test_call_m27 import _shipped_role_environment, call_configuration
from tests.integration.m2_support import database_settings
from tests.integration.role_probe import release_probe_role
from tests.integration.test_call_role_privileges_m27 import CALL_WORKER_TABLE_PRIVILEGES

pytestmark = pytest.mark.integration

DISPATCH_KEY = str(UUID(int=0x2701))
TOMBSTONE = {"call_id": CALL_ID, "erased": True, "provider_deleted": False}

# The `veetbot_call_ingress` row of docs/bland-setup.md.
CALL_INGRESS_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "alembic_version": frozenset({"SELECT"}),
    "call_records": frozenset({"SELECT", "INSERT", "UPDATE"}),
}


def _outbound(**changes: Any) -> dict[str, Any]:
    """Bland's raw GET /v1/calls/{id} view of the dispatched outbound call."""
    return {
        **provider_call(),
        **changes,
        "inbound": False,
        "to": RECIPIENT,
        "from": NUMBER,
        "created_at": NOW.isoformat(),
        "metadata": {"veetbot_request_id": DISPATCH_KEY},
    }


OUTCOMES: dict[str, tuple[dict[str, Any], str]] = {
    "busy": (_outbound(status="busy"), "busy"),
    "no-answer": (_outbound(status="no-answer"), "no_answer"),
    "voicemail": (_outbound(**voicemail_detection_hangup()), "voicemail"),
    "completed": (_outbound(status="completed", answered_by="human"), "completed"),
}


def _owner_settings() -> Settings:
    """The owner API composition with calling enabled, as `_enabled_call_settings` does."""
    credential = SecretStr(
        json.dumps({"api_key": KEY, "configuration": call_configuration().model_dump()})
    )
    return replace(
        database_settings(),
        call_enabled=True,
        call_configuration=call_configuration(),
        credentials={"bland_read": credential, "bland_call": credential},
    )


def _owner(*scopes: str) -> Principal:
    return Principal(
        tenant_id=principal().tenant_id, principal_id=principal().principal_id, scopes=set(scopes)
    )


def _role_settings(tmp_path: Path, url: str, *, ingress: bool) -> Settings:
    """Load a shipped calling role environment the way systemd does, on ``url``."""
    tmp_path.mkdir(exist_ok=True)
    configuration = tmp_path / "calls.json"
    configuration.write_text(call_configuration().model_dump_json())
    credential = tmp_path / ("webhook-secret" if ingress else "bland-api-key")
    credential.write_text(SIGNING_FIXTURE if ingress else KEY)
    credential.chmod(0o600)
    environment = {
        **_shipped_role_environment(f"veetbot-call{'-ingress' if ingress else ''}.env.example"),
        "AUTH_TENANT_ID": principal().tenant_id,
        "AUTH_PRINCIPAL_ID": principal().principal_id,
        "AGENT_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "AGENT_CALL_ENABLED": "1",
        "AGENT_CALL_INGRESS_ENABLED": "1" if ingress else "0",
        "AGENT_CALL_NOTIFICATIONS_ENABLED": "0" if ingress else "1",
        "AGENT_NOTIFICATION_API_ENABLED": "0" if ingress else "1",
        "AGENT_NOTIFICATION_DISPATCH_ENABLED": "0" if ingress else "1",
        "BLAND_CONFIGURATION_FILE": str(configuration),
        ("BLAND_WEBHOOK_SECRET_FILE" if ingress else "BLAND_API_KEY_FILE"): str(credential),
    }
    # The shipped login placeholder gives way to the probe login.
    return replace(load_call_worker_settings(environment, ingress=ingress), database_url=url)


@asynccontextmanager
async def _ingress_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The public webhook app, composed as the ingress role under its documented grants."""
    async with release_probe_role("veetbot_ingress_probe", CALL_INGRESS_TABLE_PRIVILEGES) as url:
        settings = _role_settings(tmp_path / "ingress", url, ingress=True)
        async with build_call_worker(settings=settings, ingress=True) as composition:
            assert composition.webhook_app is not None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=composition.webhook_app),
                base_url="http://ingress",
            ) as client:
                yield client


async def _reconcile_as_call_role(tmp_path: Path, raw: dict[str, Any]) -> int:
    """Run one reconciliation pass as the call role; Bland answers ``raw`` for the call."""

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        assert (server, name, args) == (
            "bland_read",
            "provider_get_call",
            {"provider_call_id": CALL_ID},
        )
        # The real adapter normalizes the provider's raw fields; only HTTP is replaced.
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=raw))
        async with httpx.AsyncClient(transport=transport) as http:
            normalized = await BlandClient(KEY, NUMBER, http_client=http).get_call(CALL_ID)
        return MCPCallResult(structured=normalized)

    async with release_probe_role("veetbot_call_probe", CALL_WORKER_TABLE_PRIVILEGES) as url:
        settings = _role_settings(tmp_path / "worker", url, ingress=False)
        async with build_call_worker(settings=settings, clock=FixedClock(NOW)) as composition:
            assert composition.worker is not None
            composition.worker.service.provider = provider
            return await composition.worker.service.reconcile()


async def _post_callback(client: httpx.AsyncClient) -> int:
    body, signature = signed_body(CALL_ID)
    response = await client.post(
        "/webhooks/bland", content=body, headers={"X-Webhook-Signature": signature}
    )
    return response.status_code


@pytest.mark.parametrize(("raw", "status"), OUTCOMES.values(), ids=OUTCOMES.keys())
async def test_outbound_call_result_flows_from_ingress_to_owner_reads(
    tmp_path: Path, raw: dict[str, Any], status: str
) -> None:
    owner = _owner("call.read", "call.delete")
    settings = _owner_settings()
    async with build(
        settings=settings, storage="postgres", principal=owner, clock=FixedClock(NOW)
    ) as app:
        # The call was dispatched moments ago: its reservation is the only trace.
        async with app.uow_factory() as uow:
            await uow.calls.put(
                CallRecord(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    kind="dispatch",
                    key=DISPATCH_KEY,
                    revision=1,
                    payload={
                        "active": True,
                        "status": "accepted",
                        "provider_call_id": CALL_ID,
                        "account_id": call_configuration().account_id,
                        "recipient_hash": hashlib.sha256(RECIPIENT.encode()).hexdigest(),
                        "run_id": str(UUID(int=0x2702)),
                        "session_id": str(UUID(int=0x2703)),
                    },
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision=0,
            )
        api = create_app(app.services, settings, owner, app.new_request_id, app.readiness_probe)
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://api"
            ) as client,
            _ingress_client(tmp_path) as ingress,
        ):
            # The provider's post-call callback lands on the public ingress.
            assert await _post_callback(ingress) == 202
            async with app.uow_factory() as uow:
                receipts = await uow.calls.list(owner, "receipt")
            assert [(r.key, r.payload) for r in receipts] == [
                (CALL_ID, {"active": True, "status": "pending"})
            ]

            # Dispatched but not yet reconciled: a content-free pending view, not 404.
            pending = await client.get(f"/v1/calls/{CALL_ID}")
            assert pending.status_code == 200
            assert pending.json() == {
                "call_id": CALL_ID,
                "direction": "outbound",
                "status": "accepted",
                "result_pending": True,
            }
            assert (await client.get("/v1/calls")).json() == {"calls": [], "next_cursor": None}

            assert await _reconcile_as_call_role(tmp_path, raw) == 1

            # The owner reads the distinct normalized outcome; the list carries no transcript.
            fetched = await client.get(f"/v1/calls/{CALL_ID}")
            assert fetched.status_code == 200
            assert fetched.headers["cache-control"] == "private, no-store"
            record = fetched.json()
            assert record["call_id"] == CALL_ID and record["status"] == status
            assert record["direction"] == "outbound" and record["request_id"] == DISPATCH_KEY
            assert record["source"] == "bland" and record["trust"] == "EXTERNAL_UNTRUSTED"
            assert record["transcript"] == raw["concatenated_transcript"]
            listed = (await client.get("/v1/calls")).json()
            assert listed["next_cursor"] is None
            assert [c["call_id"] for c in listed["calls"]] == [CALL_ID]
            assert "transcript" not in listed["calls"][0]
            assert listed["calls"][0]["status"] == status

            # PostgreSQL holds one call, a closed dispatch, a received receipt, one alert.
            async with app.uow_factory() as uow:
                calls = await uow.calls.list(owner, "call")
                dispatch = await uow.calls.get(owner, "dispatch", DISPATCH_KEY)
                receipt = await uow.calls.get(owner, "receipt", CALL_ID)
                notifications = await uow.notification_outbox.list(owner, limit=10)
            assert [c.key for c in calls] == [CALL_ID]
            assert dispatch is not None and dispatch.payload["active"] is False
            assert dispatch.payload["status"] == status
            assert receipt is not None and receipt.payload["status"] == "received"
            assert [n.kind for n in notifications] == [NotificationKind.CALL_FINISHED]
            assert notifications[0].payload.call_id == UUID(CALL_ID)

            # A duplicate callback is accepted, then produces no second result or alert.
            assert await _post_callback(ingress) == 202
            assert await _reconcile_as_call_role(tmp_path, raw) == 0
            async with app.uow_factory() as uow:
                assert len(await uow.calls.list(owner, "call")) == 1
                assert len(await uow.notification_outbox.list(owner, limit=10)) == 1

            # Owner erasure leaves a content-free tombstone that a replay cannot reopen.
            erased = await client.delete(f"/v1/calls/{CALL_ID}")
            assert erased.status_code == 200 and erased.json() == TOMBSTONE
            assert (await client.get(f"/v1/calls/{CALL_ID}")).json() == TOMBSTONE
            assert (await client.get("/v1/calls")).json()["calls"] == [TOMBSTONE]
            assert await _post_callback(ingress) == 202
            assert await _reconcile_as_call_role(tmp_path, raw) == 0
            assert (await client.get(f"/v1/calls/{CALL_ID}")).json() == TOMBSTONE
            async with app.uow_factory() as uow:
                row = await uow.calls.get(owner, "call", CALL_ID)
            assert row is not None and raw["concatenated_transcript"] not in str(row.payload)


async def test_unknown_call_id_still_answers_not_found(tmp_path: Path) -> None:
    """The pending view covers only the active dispatch's provider call id."""
    owner = _owner("call.read")
    settings = _owner_settings()
    async with build(
        settings=settings, storage="postgres", principal=owner, clock=FixedClock(NOW)
    ) as app:
        api = create_app(app.services, settings, owner, app.new_request_id, app.readiness_probe)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://api"
        ) as client:
            assert (await client.get(f"/v1/calls/{UUID(int=0x2704)}")).status_code == 404
