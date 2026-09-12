"""Bounded automatic-draft slots and historical-analysis fairness (fake providers)."""

import json
from dataclasses import replace
from typing import Any

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import Composition, build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EmailThread
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import (
    MailboxFactory,
    _assessment_turn,
    _current_mail_factory,
    _page,
)


async def _unchanged_mailbox() -> MailboxFactory:
    base = await _current_mail_factory()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "search_threads":
                value: dict[str, Any] = {"threads": []}
                return MCPCallResult(content=(json.dumps(value),), structured=value)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


async def _seed_threads(
    app: Composition, count: int, *, archived_last: bool = False
) -> list[EmailThread]:
    session = await app.sessions.create()
    threads = []
    for index in range(count):
        page = _page()
        page["thread_id"] = f"thread-{index + 1}"
        page["messages"][0].update(
            thread_id=page["thread_id"],
            id=f"message-{index + 1}",
            internal_date=1789128000000 - 1000 * index,
            label_ids=[] if archived_last and index == count - 1 else ["INBOX"],
        )
        threads.append(
            await app.services.email.import_thread(app.principal, "default", page, session)
        )
    return threads


async def test_existing_top_three_drafts_occupy_automatic_slots_on_unchanged_refresh() -> None:
    body = ScriptedTurn(text=json.dumps({"body": "Thanks. I will review the materials."}))
    script = FakeModelScript(
        turns=[
            *[_assessment_turn(needs_reply=True) for _ in range(4)],
            *[body for _ in range(3)],
            *[_assessment_turn(needs_reply=True) for _ in range(2)],
            *[body for _ in range(3)],
        ]
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=await _unchanged_mailbox(),
    ) as app:
        await _seed_threads(app, 6)
        first = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(first.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            first_drafts = await uow.email.list(app.principal, "draft")
        assert len(first_drafts) == 3
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(second.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            second_drafts = await uow.email.list(app.principal, "draft")
        assert {row.key for row in second_drafts} == {row.key for row in first_drafts}


async def test_profile_churn_cannot_starve_historical_body_assessment() -> None:
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(12)]),
        mcp_client_factory=await _unchanged_mailbox(),
    ) as app:
        threads = await _seed_threads(app, 5, archived_last=True)
        for _ in range(3):
            operation = await app.services.email.submit_task(app.principal, kind="refresh")
            assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
            await app.services.email.feedback(
                app.principal,
                thread_id=threads[0].id,
                target="thread",
                judgment="no_reply_needed",
            )
        async with app.uow_factory() as uow:
            historical_assessment = await uow.email.get(
                app.principal, "assessment", str(threads[-1].id)
            )
        assert historical_assessment is not None


async def test_deleting_cached_message_preserves_partial_thread_coverage() -> None:
    base = await _unchanged_mailbox()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "sync_changes":
                value = {
                    "schema_version": 1,
                    "history_id": "102",
                    "changes": [
                        {
                            "kind": "message_deleted",
                            "message_id": "m1",
                            "thread_id": "thread-1",
                            "label_ids": [],
                        }
                    ],
                    "next_page_token": None,
                    "resync_required": False,
                }
                return MCPCallResult(content=(json.dumps(value),), structured=value)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=factory,
    ) as app:
        session = await app.sessions.create()
        page = _page(changed=True)
        page.update(complete=False, total_messages=3)
        thread = await app.services.email.import_thread(app.principal, "default", page, session)
        assert not thread.complete and all(message.complete for message in thread.messages)
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "thread", str(thread.id))
        assert row is not None
        current = EmailThread.model_validate(row.payload)
        assert [message.id for message in current.messages] == ["m2"]
        assert not current.complete


@pytest.mark.parametrize(
    ("changes", "surviving_id"),
    [
        (
            [("message_deleted", "m1", "1000"), ("message_added", "m1", "109")],
            None,
        ),
        (
            [("message_added", "m1", None), ("message_deleted", "m1", None)],
            None,
        ),
        (
            [("message_added", "m1", "1000"), ("message_deleted", "m1", "109")],
            "m1",
        ),
        (
            [("message_added", "m2", "109"), ("message_deleted", "m1", "1000")],
            "m2",
        ),
    ],
    ids=["numeric-tombstone", "stable-order-tombstone", "later-live-event", "other-message"],
)
async def test_history_tombstones_cancel_only_earlier_changes_for_the_same_message(
    changes: list[tuple[str, str, str | None]], surviving_id: str | None
) -> None:
    base = await _unchanged_mailbox()
    thread_reads: list[str] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            value: dict[str, Any]
            if name == "sync_changes":
                value = {
                    "schema_version": 1,
                    "history_id": "1001",
                    "changes": [
                        {
                            "kind": kind,
                            "message_id": message_id,
                            "thread_id": "thread-1",
                            "label_ids": [],
                            **({"history_id": history_id} if history_id is not None else {}),
                        }
                        for kind, message_id, history_id in changes
                    ],
                    "next_page_token": None,
                    "resync_required": False,
                }
            elif name == "get_thread_page":
                thread_reads.append(arguments["thread_id"])
                if surviving_id is None:
                    return MCPCallResult(content=("gmail.provider_rejected",), is_error=True)
                value = _page()
                value["messages"][0]["id"] = surviving_id
            else:
                return await original(name, arguments)
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=factory,
    ) as app:
        session = await app.sessions.create()
        thread = await app.services.email.import_thread(app.principal, "default", _page(), session)
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        assert thread_reads == ([] if surviving_id is None else ["thread-1"])
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "thread", str(thread.id))
            account = await uow.email.get(app.principal, "account", "default")
            sync = await uow.email.get(app.principal, "sync", "default")
        assert row is not None and account is not None and sync is not None
        current = EmailThread.model_validate(row.payload)
        assert [message.id for message in current.messages] == (
            [] if surviving_id is None else [surviving_id]
        )
        assert account.payload["status"] == "ready" and account.payload["history_id"] == "1001"
        assert sync.payload["change_pending"] == []
