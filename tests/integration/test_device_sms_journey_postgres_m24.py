"""SMS through the owner's iPhone, out and in, over the HTTP API on PostgreSQL.

Guards docs/plan/device-channel-and-sms.md end to end with the real push-wake
channel rather than the fake one the gates inject:

- "The DeviceChannel port": the worker writes a pending `device_invocations`
  row, a content-free push carrying only the invocation id wakes the phone, the
  phone fetches the invocation over the authenticated API and posts exactly one
  result, and only that device-posted result can mean `sent` (hard gates 2-4).
- "device.sms.send": an owner-authored turn is a plain allow, because the
  compose-sheet tap is the confirmation.
- "SMS ingest": the phone posts `{channel, sender, body, received_at}`; the
  message enters the standing triage session as device-originated untrusted
  content, idempotent by digest, and a reply the triage turn drafts is raised
  to an approval instead of reaching the phone (hard gates 9 and 10).
- Hard gate 7: after the owner revokes the device, its next action fails.

The phone is played by this test over the three device routes while a durable
worker executes the runs; the device itself is registered over HTTP.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.push import FakePushTransport
from agent_core.bootstrap import build, build_notification_worker
from agent_core.config import Settings
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunStatus
from agent_core.tools.device_tools import DEVICE_SMS_SEND_TOOL_NAME
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings
from tests.integration.test_golden_journeys import _notification_role_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
SITTER = "+15555550123"
OWNER_REQUEST = "Text the sitter that I am running twenty minutes late."
OUTBOUND = "Running twenty minutes late, sorry."
INBOUND = "No problem. Marzipan has eaten and is asleep."
DRAFTED_REPLY = "Thank you, see you soon."
PUSH_TOKEN = "owner-iphone" + "-push-token"


class _WaitingClock(FixedClock):
    """Domain time stands still while a wait yields briefly in real time.

    `FixedClock.sleep` advances the clock, so the push-wake channel's bounded
    wait would run out before the phone in this test could answer. Here the
    invocation stays inside its timeout until the phone posts a result.
    """

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(min(seconds, 0.02))


def _settings() -> Settings:
    return replace(
        database_settings(),
        device_channel_enabled=True,
        device_sms_enabled=True,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )


def _send_turn(body: str, call_id: str) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name=DEVICE_SMS_SEND_TOOL_NAME,
                arguments={"recipient": SITTER, "body": body},
                call_id=call_id,
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )


async def _pending(client: httpx.AsyncClient, device_id: str) -> list[dict[str, Any]]:
    fetched = await client.get(f"/v1/devices/{device_id}/invocations")
    assert fetched.status_code == 200, fetched.text
    assert fetched.headers["cache-control"] == "private, no-store"
    invocations: list[dict[str, Any]] = fetched.json()["invocations"]
    return invocations


async def _await_invocation(client: httpx.AsyncClient, device_id: str) -> dict[str, Any]:
    """Poll the fetch route the way a woken phone does, for at most a minute."""
    for _ in range(3000):
        invocations = await _pending(client, device_id)
        if invocations:
            [invocation] = invocations
            return invocation
        await asyncio.sleep(0.02)
    raise AssertionError("the worker never asked the phone to send")


def _inbound_message() -> dict[str, str]:
    return {
        "channel": "sms",
        "sender": SITTER,
        "body": INBOUND,
        "received_at": NOW.isoformat(),
    }


async def test_an_sms_goes_out_by_the_owners_phone_and_a_reply_comes_back_untrusted(
    tmp_path: Path,
) -> None:
    """Register, send through the phone, receive, refuse the triage reply, revoke."""
    settings = _settings()
    clock = _WaitingClock(NOW)
    transport = FakePushTransport()
    script = FakeModelScript(
        turns=[
            _send_turn(OUTBOUND, "owner-send"),
            ScriptedTurn(text="I texted the sitter."),
            _send_turn(DRAFTED_REPLY, "triage-reply"),
            ScriptedTurn(text="I left the sitter's message unanswered."),
        ]
    )
    async with (
        build(settings=settings, storage="postgres", script=script, clock=clock) as app,
        build_notification_worker(
            settings=_notification_role_settings(tmp_path), transport=transport, clock=clock
        ) as notifier,
        api_client(app) as client,
    ):
        registered = await client.post(
            "/v1/devices",
            headers={"Idempotency-Key": "register-owner-iphone"},
            json={
                "client_device_id": "owner-iphone",
                "name": "Owner iPhone",
                "kind": "mobile",
                "platform": "ios",
                "app_bundle_id": "com.veetbot.app",
                "push_provider": "apns",
                "push_token": PUSH_TOKEN,
                "push_environment": "sandbox",
                "muted_kinds": [],
                "capabilities": [DEVICE_SMS_SEND_TOOL_NAME],
            },
        )
        assert registered.status_code == 201, registered.text
        device_id = registered.json()["id"]
        assert registered.json()["capabilities"] == [DEVICE_SMS_SEND_TOOL_NAME]
        assert await _pending(client, device_id) == []

        # --- Outbound: the owner asks in chat, the phone performs the send. ---
        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        asked = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"Idempotency-Key": "text-the-sitter"},
            json={"content": [{"type": "text", "text": OWNER_REQUEST}]},
        )
        assert asked.status_code == 202, asked.text
        run_id = UUID(asked.json()["run_id"])
        working = asyncio.create_task(run_worker_once(app, "device-sms-outbound"))
        try:
            invocation = await _await_invocation(client, device_id)
            assert invocation["tool_name"] == DEVICE_SMS_SEND_TOOL_NAME
            assert invocation["arguments"] == {"recipient": SITTER, "body": OUTBOUND}

            # Nothing has been sent: the run is still working and no approval
            # stood between the owner's request and the phone.
            waiting = await client.get(f"/v1/runs/{run_id}")
            assert waiting.json()["status"] == RunStatus.RUNNING.value, waiting.text
            approvals = await client.get("/v1/approvals", params={"run_id": str(run_id)})
            assert approvals.json()["items"] == []
            assert not working.done()

            # The wake that reaches Apple names the invocation and carries no content.
            assert await notifier.run_once() == 1
            [(target, wake)] = transport.calls
            assert target.token.get_secret_value() == PUSH_TOKEN
            pushed = wake.payload.model_dump(mode="json")
            assert pushed["invocation_id"] == invocation["id"]
            for private in (SITTER, OUTBOUND, OWNER_REQUEST):
                assert private not in json.dumps(pushed)

            sent = await client.post(
                f"/v1/devices/{device_id}/invocations/{invocation['id']}/result",
                json={"status": "sent"},
            )
            assert sent.status_code == 200, sent.text
            assert sent.json()["id"] == invocation["id"] and sent.json()["status"] == "sent"
            # One result per invocation: a later, different answer changes nothing.
            disagreed = await client.post(
                f"/v1/devices/{device_id}/invocations/{invocation['id']}/result",
                json={"status": "failed"},
            )
            assert disagreed.json() == sent.json()
            assert await asyncio.wait_for(working, timeout=60)
        finally:
            working.cancel()

        finished = await client.get(f"/v1/runs/{run_id}")
        assert finished.json()["status"] == RunStatus.COMPLETED.value, finished.text
        transcript = await client.get(f"/v1/sessions/{session_id}/messages")
        assert transcript.json()["items"][-1]["content"] == [
            {"type": "text", "text": "I texted the sitter."}
        ]
        assert await _pending(client, device_id) == []
        events = await app.runs.events(run_id)
        completed = [
            event
            for event in events
            if event.event_type == "tool.call.completed"
            and event.payload.get("name") == DEVICE_SMS_SEND_TOOL_NAME
        ]
        assert len(completed) == 1, [event.event_type for event in events]

        # --- Inbound: the sitter's reply arrives through the phone. ---
        accepted = await client.post(f"/v1/devices/{device_id}/messages", json=_inbound_message())
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["duplicate"] is False
        assert INBOUND not in accepted.text
        triage_session = accepted.json()["session_id"]
        triage_run = UUID(accepted.json()["run_id"])
        assert triage_session != session_id
        replayed = await client.post(f"/v1/devices/{device_id}/messages", json=_inbound_message())
        assert replayed.status_code == 202, replayed.text
        assert replayed.json() == {**accepted.json(), "duplicate": True}

        # The triage turn drafts a reply. Its origin is untrusted, so the send
        # is raised to the owner instead of reaching the phone.
        assert await run_worker_once(app, "device-sms-triage")
        parked = await client.get(f"/v1/runs/{triage_run}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.text
        listed = await client.get("/v1/approvals", params={"run_id": str(triage_run)})
        [approval] = listed.json()["items"]
        assert approval["tool_name"] == DEVICE_SMS_SEND_TOOL_NAME
        # The card describes the action without repeating the recipient or the body.
        assert DRAFTED_REPLY not in listed.text and SITTER not in listed.text
        assert await _pending(client, device_id) == []
        denied = await client.post(
            f"/v1/approvals/{approval['id']}/resolve", json={"decision": "deny"}
        )
        assert denied.status_code == 200, denied.text
        assert await run_worker_once(app, "device-sms-triage-resume")
        closed = await client.get(f"/v1/runs/{triage_run}")
        assert closed.json()["status"] == RunStatus.COMPLETED.value, closed.text
        assert await _pending(client, device_id) == []

        async with app.uow_factory() as uow:
            triage_events = await uow.events.list_after(UUID(triage_session), 0, app.principal)
        seeds = [event for event in triage_events if event.event_type == "user.message.created"]
        assert len(seeds) == 1, [event.event_type for event in triage_events]
        assert seeds[0].actor_type == "device"
        assert seeds[0].payload["trust"] == "external_untrusted"
        assert INBOUND in json.dumps(seeds[0].payload)
        # The body lives in the designated content event and nowhere else.
        elsewhere = [event for event in triage_events if event is not seeds[0]]
        assert not [event.event_type for event in elsewhere if INBOUND in json.dumps(event.payload)]

    # --- After a restart the owner revokes the phone; its next action fails. ---
    async with (
        build(settings=settings, storage="postgres", clock=clock) as restarted,
        api_client(restarted) as client,
    ):
        duplicate = await client.post(f"/v1/devices/{device_id}/messages", json=_inbound_message())
        assert duplicate.status_code == 202, duplicate.text
        assert duplicate.json() == {**accepted.json(), "duplicate": True}
        revoked = await client.post(f"/v1/devices/{device_id}/revoke")
        assert revoked.status_code == 200, revoked.text
        fetch = await client.get(f"/v1/devices/{device_id}/invocations")
        assert fetch.status_code == 409, fetch.text
        assert fetch.json()["error"]["details"]["reason"] == "device_revoked"
        later = await client.post(
            f"/v1/devices/{device_id}/messages",
            json={**_inbound_message(), "body": "Are you nearly here?"},
        )
        assert later.status_code == 409, later.text
        assert "nearly here" not in later.text
        answer = await client.post(
            f"/v1/devices/{device_id}/invocations/{invocation['id']}/result",
            json={"status": "cancelled"},
        )
        assert answer.status_code == 409, answer.text
