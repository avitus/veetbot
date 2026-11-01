"""An owner-edited draft is sent once, through approval and the worker, on PostgreSQL.

Guards docs/plan/email-experience.md "Editing and sending" (optimistic draft
revisions and the explicit conflict, the frozen exact action, the ordinary
approval, the governed freshness reread before one dispatch), the command
idempotency rule (email-experience.md:827), the interactive lane for send work
(email-experience.md:875-877) and the retention rule that removes sent draft
bodies after 30 days (email-experience.md:912), end to end over the HTTP
routes, the approval route, the durable worker, a composition restart and the
maintenance sweep, with a recording fake Gmail send server.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EMAIL_BODY_RETENTION
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _current_mail_factory, _seed_draft
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 12, 9, tzinfo=UTC)
FINAL_BODY = "Thanks. I reviewed the materials and approve them."
SEND_KEY = "send-draft-once"


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _recording_factory(writes: list[tuple[str, dict[str, Any]]]) -> Any:
    """Serve governed Gmail reads from the current mailbox and record the one send."""
    base = await _current_mail_factory()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name in {"send_message", "reply"}:
                writes.append((config.server_id, arguments))
                receipt = {"message_id": "sent-1", "thread_id": arguments["thread_id"]}
                return MCPCallResult(content=(json.dumps(receipt),), structured=receipt)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


async def test_edited_draft_is_sent_once_through_approval_worker_and_restart() -> None:
    """One provider write carries the final revision; replays and stale edits never add one."""
    writes: list[tuple[str, dict[str, Any]]] = []
    factory = await _recording_factory(writes)
    clock = FixedClock(NOW)
    settings = replace(
        _email_settings(),
        database_url=database_settings().database_url,
        email_mode_enabled=True,
    )
    async with (
        build(
            settings=settings, storage="postgres", mcp_client_factory=factory, clock=clock
        ) as app,
        _client(app) as client,
    ):
        thread, draft = await _seed_draft(app)
        inbox = await client.get("/v1/email/threads", params={"view": "all"})
        assert inbox.status_code == 200, inbox.text
        assert [item["id"] for item in inbox.json()["items"]] == [str(thread.id)]
        detail = await client.get(f"/v1/email/threads/{thread.id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["draft"]["id"] == str(draft.id)
        [message] = detail.json()["messages"]
        assert message["body"] == "Please approve the board materials."
        assert message["reader_body"]

        edit = {
            "expected_revision": draft.revision,
            "to": ["reply@example.test"],
            "cc": ["board@example.test"],
            "subject": "Re: Decision",
            "body": FINAL_BODY,
        }
        edited = await client.put(f"/v1/email/drafts/{draft.id}", json=edit)
        assert edited.status_code == 200, edited.text
        assert edited.json()["revision"] == 2 and edited.json()["owner_edited"] is True
        stale = await client.put(
            f"/v1/email/drafts/{draft.id}", json={**edit, "body": "A stale second-device edit."}
        )
        assert stale.status_code == 409, stale.text
        current = await client.get(f"/v1/email/drafts/{draft.id}")
        assert current.json()["revision"] == 2 and current.json()["body"] == FINAL_BODY

        proposed = await client.post(
            f"/v1/email/drafts/{draft.id}/send-proposal",
            json={"expected_revision": 2},
            headers={"Idempotency-Key": SEND_KEY},
        )
        assert proposed.status_code == 200, proposed.text
        assert proposed.json()["status"] == RunStatus.QUEUED.value
        assert proposed.json()["approval_id"] is None
        run_id = UUID(proposed.json()["run_id"])
        assert writes == []

        # The owner waits on a send, so it joins the interactive lane and never
        # queues behind a refresh (email-experience.md:875-877).
        asynchronous = cast(DurableWorker, app.async_worker_factory("async-lane"))
        assert not await asynchronous.run_once()
        interactive = cast(DurableWorker, app.worker_factory("interactive-lane"))
        assert await interactive.run_once()
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        awaiting = await client.get(f"/v1/email/drafts/{draft.id}")
        assert awaiting.json()["status"] == "awaiting_approval"
        approval_id = UUID(awaiting.json()["approval_id"])
        listed = await client.get("/v1/approvals", params={"run_id": str(run_id)})
        [approval] = listed.json()["items"]
        assert UUID(approval["id"]) == approval_id
        assert approval["tool_name"].endswith("send_message")
        assert approval["arguments"]["body"] == FINAL_BODY
        assert approval["arguments"]["to"] == "reply@example.test"
        assert approval["arguments"]["in_reply_to"] == "<m1@example.test>"
        resolved = await client.post(
            f"/v1/approvals/{approval_id}/resolve", json={"decision": "approve_once"}
        )
        assert resolved.status_code == 200, resolved.text
        assert writes == []

    async with (
        build(
            settings=settings, storage="postgres", mcp_client_factory=factory, clock=clock
        ) as app,
        _client(app) as client,
    ):
        task = await app.services.email.get_task(app.principal, run_id)
        assert task is not None and task.kind == "send" and task.draft_id == draft.id
        interactive = cast(DurableWorker, app.worker_factory("interactive-lane-after-restart"))
        assert await interactive.run_once()
        run = await app.runs.get(run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert writes == [
            (
                "gmail_send",
                {
                    "to": "reply@example.test",
                    "cc": "board@example.test",
                    "bcc": None,
                    "subject": "Re: Decision",
                    "body": FINAL_BODY,
                    "thread_id": "thread-1",
                    "in_reply_to": "<m1@example.test>",
                    "references": "<root@example.test> <m1@example.test>",
                },
            )
        ]
        operation = await client.get(f"/v1/email/operations/{task.id}")
        assert operation.status_code == 200, operation.text
        assert operation.json()["status"] == RunStatus.COMPLETED.value
        assert operation.json()["run_id"] == str(run_id)
        sent = await client.get(f"/v1/email/drafts/{draft.id}")
        assert sent.json()["status"] == "sent" and sent.json()["body"] == FINAL_BODY
        assert sent.json()["revision"] == 2

        replay = await client.post(
            f"/v1/email/drafts/{draft.id}/send-proposal",
            json={"expected_revision": 2},
            headers={"Idempotency-Key": SEND_KEY},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["run_id"] == str(run_id)
        assert replay.json()["status"] == RunStatus.COMPLETED.value
        changed = await client.post(
            f"/v1/email/drafts/{draft.id}/send-proposal",
            json={"expected_revision": 1},
            headers={"Idempotency-Key": SEND_KEY},
        )
        assert changed.status_code == 409, changed.text
        assert len(writes) == 1

        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, app.principal)
            [send] = [item for item in invocations if item.tool_name.endswith("send_message")]
            approval_row = await uow.approvals.get_by_action(send.id)
            assert approval_row is not None and approval_row.status is ApprovalStatus.APPROVED
            assert approval_row.resolved_by == app.principal.principal_id
            events = await uow.events.list_after(run.session_id, 0, app.principal)
            row = await uow.email.get(app.principal, "draft", str(draft.id))
        requested = [event for event in events if event.event_type == "approval.requested"]
        done = [event for event in events if event.event_type == "approval.resolved"]
        assert len(requested) == len(done) == 1 and requested[0].sequence < done[0].sequence
        assert row is not None and row.payload["status"] == "sent"
        assert row.payload["body"] == FINAL_BODY

    # Sent draft bodies are removed after 30 days (email-experience.md:912).
    clock.advance(EMAIL_BODY_RETENTION + timedelta(seconds=1))
    async with (
        build(
            settings=settings, storage="postgres", mcp_client_factory=factory, clock=clock
        ) as app,
        _client(app) as client,
    ):
        await cast(MaintenanceWorker, app.maintenance_factory()).run_once()
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "draft", str(draft.id))
        assert row is not None and row.payload["status"] == "sent"
        assert row.payload["body"] == ""
        expired = await client.get(f"/v1/email/drafts/{draft.id}")
        assert expired.status_code == 200, expired.text
        assert expired.json()["status"] == "sent" and expired.json()["body"] == ""
        revisions = await client.get(f"/v1/email/drafts/{draft.id}/revisions")
        assert revisions.status_code == 200 and revisions.json()["items"] == []
    assert len(writes) == 1


async def test_one_press_send_waits_out_its_undo_window_then_sends_once() -> None:
    """A consented send stays parked until its window closes; Undo inside it sends nothing,
    and a second press after the window sends exactly one message (ADR-0276)."""
    writes: list[tuple[str, dict[str, Any]]] = []
    factory = await _recording_factory(writes)
    clock = FixedClock(NOW)
    settings = replace(
        _email_settings(),
        database_url=database_settings().database_url,
        email_mode_enabled=True,
    )
    async with (
        build(
            settings=settings, storage="postgres", mcp_client_factory=factory, clock=clock
        ) as app,
        _client(app) as client,
    ):
        thread, draft = await _seed_draft(app)
        interactive = cast(DurableWorker, app.worker_factory("interactive-lane"))
        pressed = await client.post(
            f"/v1/email/drafts/{draft.id}/send",
            json={
                "expected_revision": draft.revision,
                "source_revision": thread.revision,
                "idempotency_key": "press-one",
            },
            headers={"Idempotency-Key": "press-one"},
        )
        assert pressed.status_code == 200, pressed.text
        assert pressed.json()["draft"]["status"] == "sending"
        assert pressed.json()["draft"]["send_undo_until"] is not None
        # Parked: the worker will not claim it before the window closes.
        assert not await interactive.run_once()
        undone = await client.post(
            f"/v1/email/drafts/{draft.id}/send-undo", json={"expected_revision": draft.revision}
        )
        assert undone.status_code == 200, undone.text
        assert undone.json()["status"] == "ready" and undone.json()["send_undo_until"] is None
        clock.advance(timedelta(seconds=11))
        assert not await interactive.run_once()
        assert writes == []

        again = await client.post(
            f"/v1/email/drafts/{draft.id}/send",
            json={
                "expected_revision": draft.revision,
                "source_revision": thread.revision,
                "idempotency_key": "press-two",
            },
            headers={"Idempotency-Key": "press-two"},
        )
        assert again.status_code == 200, again.text
        assert not await interactive.run_once()
        clock.advance(timedelta(seconds=11))
        assert await interactive.run_once()
        run = await app.runs.get(UUID(again.json()["run_id"]))
        assert run.status is RunStatus.COMPLETED, run.failure
        sent = await client.get(f"/v1/email/drafts/{draft.id}")
        assert sent.json()["status"] == "sent" and sent.json()["send_undo_until"] is None
        assert len(writes) == 1 and writes[0][1]["body"] == draft.body
        late = await client.post(
            f"/v1/email/drafts/{draft.id}/send-undo", json={"expected_revision": draft.revision}
        )
        assert late.status_code == 409, late.text
