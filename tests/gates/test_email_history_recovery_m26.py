"""History overflow resumes despite a disappeared source on an earlier page."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EmailAccount
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import (
    _assessment_turn,
    _mailbox_factory,
    _profile,
    _seed_draft,
)


async def test_later_history_page_tombstone_unblocks_failed_earlier_source() -> None:
    base = await _mailbox_factory([])
    pages: list[str | None] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            value: dict[str, Any]
            if name == "get_profile":
                value = _profile()
            elif name == "search_threads":
                value = {"threads": []}
            elif name == "get_thread_page":
                return MCPCallResult(content=("gmail.provider_rejected",), is_error=True)
            else:
                assert name == "sync_changes"
                token = arguments.get("page_token")
                pages.append(token)
                value = {
                    "schema_version": 1,
                    "history_id": "102",
                    "resync_required": False,
                    "next_page_token": "later" if token is None else None,
                    "changes": [
                        {
                            "history_id": "101" if token is None else "102",
                            "kind": "message_added" if token is None else "message_deleted",
                            "thread_id": "thread-1",
                            "message_id": "m1",
                            "label_ids": [],
                        }
                    ],
                }
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn()]),
    ) as app:
        await _seed_draft(app)
        for _ in range(3):
            operation = await app.services.email.submit_task(app.principal, kind="refresh")
            assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                account_row = await uow.email.get(app.principal, "account", "default")
            assert account_row is not None
            if EmailAccount.model_validate(account_row.payload).history_id == "102":
                break
        assert pages == [None, "later"]
        assert account_row is not None
        account = EmailAccount.model_validate(account_row.payload)
        assert account.status == "ready" and account.history_id == "102"
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
            [sync] = await uow.email.list(app.principal, "sync")
        assert thread.payload["messages"] == []
        assert sync.payload["change_pending"] == []


async def test_history_lookahead_ceiling_restarts_sync_without_discarding_cached_mail(
    monkeypatch: Any,
) -> None:
    from agent_core.runtime import email_tasks

    monkeypatch.setattr(email_tasks, "EMAIL_CHANGE_EVENT_LIMIT", 2, raising=False)
    base = await _mailbox_factory([])

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            value: dict[str, Any]
            if name == "get_profile":
                value = _profile()
            elif name == "search_threads":
                value = {"threads": []}
            else:
                assert name == "sync_changes"
                value = {
                    "schema_version": 1,
                    "history_id": "105",
                    "resync_required": False,
                    "next_page_token": "later",
                    "changes": [
                        {
                            "history_id": "105",
                            "kind": "message_added",
                            "thread_id": f"t{i}",
                            "message_id": f"m{i}",
                            "label_ids": [],
                        }
                        for i in range(3)
                    ],
                }
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn()]),
    ) as app:
        await _seed_draft(app)
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "account")
            [sync] = await uow.email.list(app.principal, "sync")
            [thread] = await uow.email.list(app.principal, "thread")
        account = EmailAccount.model_validate(row.payload)
        assert account.inbox_complete is False and account.history_id is None
        assert sync.payload["change_events"] == {}
        assert sync.payload["change_pending"] == []
        messages = thread.payload["messages"]
        assert isinstance(messages, list) and len(messages) == 1
