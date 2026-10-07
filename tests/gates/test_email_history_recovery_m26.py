"""History recovery: disappeared sources, the bounded lookahead, and resynchronization."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import Composition, build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EmailAccount, EmailSyncState
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import (
    _assessment_turn,
    _mailbox_factory,
    _page,
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
        # The resync re-lists the (empty) inbox in the same slice and resumes changes
        # from the watermark read before that listing, not from the overflowed cursor.
        assert account.inbox_complete is True and account.history_id == "100"
        assert account.inbox_reached_at is None
        assert sync.payload["change_events"] == {}
        assert sync.payload["change_pending"] == []
        messages = thread.payload["messages"]
        assert isinstance(messages, list) and len(messages) == 1


def _thread_page(thread_id: str) -> dict[str, Any]:
    value = _page()
    value["thread_id"] = thread_id
    value["messages"][0].update(thread_id=thread_id, id=f"{thread_id}-m")
    return value


def _missing_thread(thread_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "thread_id": thread_id,
        "thread_missing": True,
        "history_id": None,
        "total_messages": 0,
        "returned_messages": 0,
        "messages": [],
        "next_page_token": None,
        "complete": False,
        "source_changed": False,
    }


def _change(history_id: int, thread_id: str, kind: str = "message_added") -> dict[str, Any]:
    return {
        "history_id": str(history_id),
        "kind": kind,
        "thread_id": thread_id,
        "message_id": f"{thread_id}-m",
        "label_ids": ["INBOX"],
    }


async def _backlog_mailbox(
    history: Callable[[int], tuple[list[dict[str, Any]], str | None]],
    *,
    inbox: tuple[str, ...] = (),
    missing: frozenset[str] = frozenset(),
) -> tuple[Callable[..., ScriptedMCPClient], list[str | None], list[str]]:
    """Serve numbered history pages; record every page token and full thread read."""
    base = await _mailbox_factory([])
    pages: list[str | None] = []
    reads: list[str] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            value: dict[str, Any]
            if name == "get_profile":
                value = _profile()
            elif name == "search_threads":
                listed = inbox if "in:inbox" in arguments["query"] else ()
                value = {"threads": [{"thread_id": item} for item in listed]}
            elif name == "get_thread_page":
                thread_id = arguments["thread_id"]
                reads.append(thread_id)
                value = (
                    _missing_thread(thread_id) if thread_id in missing else _thread_page(thread_id)
                )
            else:
                assert name == "sync_changes"
                pages.append(arguments.get("page_token"))
                changes, following = history(len(pages))
                value = {
                    "schema_version": 1,
                    "history_id": "200",
                    "resync_required": False,
                    "next_page_token": following,
                    "changes": changes,
                }
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    return factory, pages, reads


async def _state(app: Composition) -> tuple[EmailAccount, EmailSyncState, dict[str, Any]]:
    async with app.uow_factory() as uow:
        account = await uow.email.get(app.principal, "account", "default")
        sync = await uow.email.get(app.principal, "sync", "default")
        rows = await uow.email.list(app.principal, "thread")
    assert account is not None and sync is not None
    return (
        EmailAccount.model_validate(account.payload),
        EmailSyncState.model_validate(sync.payload),
        {str(row.payload["provider_thread_id"]): row.payload for row in rows},
    )


async def test_vanished_threads_complete_instead_of_blocking_newer_mail() -> None:
    """A conversation Gmail no longer has leaves the change queue as removed mail."""
    factory, _, reads = await _backlog_mailbox(
        lambda _: (
            [
                _change(101, "gone"),
                _change(102, "thread-1", "labels_removed"),
                _change(103, "fresh"),
            ],
            None,
        ),
        missing=frozenset({"gone", "thread-1"}),
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(4)]),
    ) as app:
        await _seed_draft(app)
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        account, sync, threads = await _state(app)
    assert sorted(reads) == ["fresh", "gone", "thread-1"]
    assert account.status == "ready" and account.error is None
    assert account.history_id == "200"
    assert sync.change_pending == [] and sync.change_events == {}
    assert "gone" not in threads
    # The cached conversation is removed exactly as deletions of its messages would.
    assert threads["thread-1"]["messages"] == [] and threads["thread-1"]["in_inbox"] is False
    assert threads["fresh"]["messages"] and threads["fresh"]["in_inbox"] is True


async def test_history_backlog_is_coalesced_and_read_within_one_slice() -> None:
    """Further pages share the slice's read allowance, and the newest change is read first."""
    following = {1: "page-1", 2: "page-2", 3: None}
    factory, pages, reads = await _backlog_mailbox(
        lambda number: ([_change(100 + number, f"t{number}")], following[number])
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(4)]),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        account, sync, _ = await _state(app)
    assert pages == [None, "page-1", "page-2"]
    # Catch-up is unfinished, so new mail has four reads; two further pages spent two.
    assert reads == ["t3", "t2"]
    assert sync.change_pending == ["t1"]
    assert sync.change_page_open is True and account.history_id == "100"


async def test_history_backlog_past_the_page_ceiling_resynchronizes_from_the_inbox(
    monkeypatch: Any,
) -> None:
    """An unfinished lookahead is bounded in pages too; the inbox re-list reads new mail."""
    from agent_core.runtime import email_tasks

    monkeypatch.setattr(email_tasks, "EMAIL_CHANGE_PAGE_LIMIT", 3, raising=False)
    factory, pages, reads = await _backlog_mailbox(
        lambda number: ([_change(100 + number, f"old-{number}")], f"page-{number}"),
        inbox=("newest", "older"),
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(4)]),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        account, sync, threads = await _state(app)
    assert pages == [None, "page-1", "page-2"]
    assert reads == ["newest", "older"]
    # Changes resume from the watermark read before the re-listing, not the abandoned cursor.
    assert account.history_id == "100" and account.inbox_complete is True
    assert sync.change_events == {} and sync.change_pending == []
    assert sync.change_cursor is None and sync.change_pages == 0
    assert threads["newest"]["in_inbox"] is True


async def test_refresh_discovers_new_mail_before_draining_older_change_reads() -> None:
    """A completed history page with pending reads cannot hide the next delta."""
    factory, pages, reads = await _backlog_mailbox(
        lambda number: (
            [_change(101 + index, f"old-{index}") for index in range(12)]
            if number == 1
            else [_change(201, "fresh")],
            None,
        )
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(20)]),
    ) as app:
        first = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(first.run_id)).status is RunStatus.COMPLETED
        _, before, _ = await _state(app)
        assert before.change_pending and before.change_page_open
        read_count = len(reads)
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(second.run_id)).status is RunStatus.COMPLETED
        _, after, _ = await _state(app)
    assert pages == [None, None]
    assert reads[read_count] == "fresh"
    assert set(after.change_pending) | set(reads) == {"fresh", *(f"old-{i}" for i in range(12))}
