"""Governed Email task execution; no provider socket or live mailbox."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient, ScriptedMCPClientFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import Composition, build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EmailDraft, EmailThread
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig, ScriptedMCPResponse
from agent_core.domain.messages import AssistantMessage, ScriptedTurn, TextPart, ToolResultItem
from agent_core.domain.runs import OutcomeKind, RunOutcome, RunStatus
from agent_core.runtime.loop import RunContext
from tests.gates.test_runtime_m4 import settings


class MailboxFactory(Protocol):
    def __call__(
        self, config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient: ...


async def test_typed_task_hook_runs_before_pending_tool_recovery(tmp_path: Path) -> None:
    seen: list[str] = []

    async def task(context: RunContext) -> RunOutcome:
        seen.append("task")
        return RunOutcome(
            kind=OutcomeKind.COMPLETED,
            final_message=AssistantMessage(content=[TextPart(text="typed operation complete")]),
        )

    async def pending(context: RunContext) -> None:
        seen.append("pending")
        raise AssertionError("typed tasks own their recovery before generic tool replay")

    async with build(settings=settings(tmp_path)) as app:
        app.executor._task_runner = task
        vars(app.executor)["_resume_pending_tools"] = pending
        run_id = await app.runs.submit("ordinary invocation for executor boundary")
        run = await app.runs.get(run_id)
        assert run.status is RunStatus.COMPLETED
        assert seen == ["task"]
        assert run.model_call_count == 0


async def test_refresh_uses_governed_mailbox_reads_without_owner_prompt_or_model() -> None:
    from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
    from agent_core.domain.mcp import MCPCallResult, ScriptedMCPResponse, ScriptedMCPServer
    from tests.gates.test_email_m18 import _email_settings, _generated_gmail_discovery

    def response(name: str, value: dict[str, Any]) -> ScriptedMCPResponse:
        import json

        return ScriptedMCPResponse(
            name=name, result=MCPCallResult(content=(json.dumps(value),), structured=value)
        )

    factory = ScriptedMCPClientFactory(
        {
            "gmail_read": ScriptedMCPServer(
                name="gmail_read",
                discovery=await _generated_gmail_discovery("read"),
                responses=(
                    response(
                        "get_profile",
                        {
                            "schema_version": 1,
                            "email_address": "owner@example.test",
                            "verified_addresses": ["owner@example.test"],
                            "history_id": "100",
                            "messages_total": 0,
                            "threads_total": 0,
                        },
                    ),
                    response("search_threads", {"threads": []}),
                    response(
                        "sync_changes",
                        {
                            "schema_version": 1,
                            "history_id": "100",
                            "next_page_token": None,
                            "changes": [],
                            "resync_required": False,
                        },
                    ),
                    response("search_threads", {"threads": []}),
                ),
            ),
            **{
                f"gmail_{mode}": ScriptedMCPServer(
                    name=f"gmail_{mode}", discovery=await _generated_gmail_discovery(mode)
                )
                for mode in ("write", "send")
            },
        }
    )
    from dataclasses import replace

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        account = _items(await app.services.email.accounts(app.principal))[0]
        assert account["status"] == "ready"
        assert account["email_address"] == "owner@example.test"
        assert account["inbox_complete"] is True
        assert account["history_complete"] is False
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 0
        assert run.tool_call_count == 4
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal)
            state = await uow.checkpoints.latest(run.id)
        assert not any(event.event_type == "user.message.received" for event in events)
        assert state is not None
        assert "mcp.gmail_read.get_profile" not in state.pinned_tool_names


async def _mailbox_factory(
    read_values: list[tuple[str, dict[str, Any]]], *, send_uncertain: bool = False
) -> ScriptedMCPClientFactory:
    import json

    from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
    from agent_core.domain.mcp import MCPCallResult, ScriptedMCPResponse, ScriptedMCPServer
    from tests.gates.test_email_m18 import _generated_gmail_discovery

    return ScriptedMCPClientFactory(
        {
            "gmail_read": ScriptedMCPServer(
                name="gmail_read",
                discovery=await _generated_gmail_discovery("read"),
                responses=tuple(
                    ScriptedMCPResponse(
                        name=name,
                        result=MCPCallResult(
                            content=(json.dumps(value),),
                            structured=value,
                            is_error=value.get("is_error", False),
                        ),
                    )
                    for name, value in read_values
                ),
            ),
            "gmail_write": ScriptedMCPServer(
                name="gmail_write", discovery=await _generated_gmail_discovery("write")
            ),
            "gmail_send": ScriptedMCPServer(
                name="gmail_send",
                discovery=await _generated_gmail_discovery("send"),
                responses=(
                    ScriptedMCPResponse(
                        name="send_message",
                        outcome="disconnect" if send_uncertain else "result",
                        result=MCPCallResult(
                            content=('{"message_id":"sent-1","thread_id":"thread-1"}',),
                            structured={"message_id": "sent-1", "thread_id": "thread-1"},
                        ),
                    ),
                ),
            ),
        }
    )


def _profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "email_address": "owner@example.test",
        "verified_addresses": ["owner@example.test"],
        "history_id": "100",
        "messages_total": 1,
        "threads_total": 1,
    }


def _page(*, changed: bool = False) -> dict[str, Any]:
    message = {
        "id": "m1",
        "thread_id": "thread-1",
        "from": "Colleague <colleague@example.test>",
        "to": "owner@example.test",
        "cc": "board@example.test",
        "reply_to": "reply@example.test",
        "subject": "Decision",
        "date": "Fri, 11 Sep 2026 12:00:00 +0000",
        "internal_date": 1789128000000,
        "history_id": "100",
        "body": "Please approve the board materials.",
        "body_complete": True,
        "headers_complete": True,
        "body_available": True,
        "label_ids": ["INBOX"],
        "direction": "received",
        "message_id_header": "<m1@example.test>",
        "references": "<root@example.test>",
        "attachments": [],
    }
    messages = [message]
    if changed:
        messages.append(
            {
                **message,
                "id": "m2",
                "from": "owner@example.test",
                "direction": "sent",
                "label_ids": ["SENT"],
                "internal_date": 1789128001000,
                "body": "Already replied elsewhere.",
            }
        )
    return {
        "schema_version": 1,
        "thread_id": "thread-1",
        "history_id": "101" if changed else "100",
        "total_messages": len(messages),
        "returned_messages": len(messages),
        "messages": messages,
        "next_page_token": None,
        "complete": True,
        "source_changed": False,
    }


async def _seed_draft(app: Composition) -> tuple[EmailThread, EmailDraft]:
    from uuid import uuid4

    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailAccount

    principal = app.principal
    async with app.uow_factory() as uow, uow.email.lock(principal):
        await save_value(
            uow.email,
            principal,
            "account",
            "default",
            EmailAccount(
                id="default",
                label="Personal",
                email_address="owner@example.test",
                verified_addresses=["owner@example.test"],
            ),
            app.clock.now(),
        )
    session = await app.sessions.create()
    thread = await app.services.email.import_thread(principal, "default", _page(), session)
    draft = await app.services.email.save_generated_draft(
        principal, thread.id, thread.revision, "Thanks. I will review them.", run_id=uuid4()
    )
    return thread, draft


@pytest.mark.parametrize("profile_address", ["owner@example.test", "OWNER@EXAMPLE.TEST"])
async def test_send_freezes_exact_value_and_rechecks_source_after_approval(
    profile_address: str,
) -> None:
    from dataclasses import replace

    from agent_core.domain.approvals import ApprovalResolutionType
    from tests.gates.test_email_m18 import _email_settings

    factory = await _mailbox_factory(
        [
            ("get_profile", {**_profile(), "email_address": profile_address}),
            ("get_thread_page", _page()),
            ("get_profile", {**_profile(), "email_address": profile_address}),
            ("get_thread_page", _page()),
        ]
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        _thread, draft = await _seed_draft(app)
        operation = await app.services.email.submit_task(
            app.principal, kind="send", draft_id=draft.id, expected_revision=draft.revision
        )
        assert (await app.runs.get(operation.run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        [approval] = await app.approvals.list_pending(run_id=operation.run_id)
        assert approval.arguments == {
            "to": "reply@example.test",
            "cc": "board@example.test",
            "bcc": None,
            "subject": "Re: Decision",
            "body": "Thanks. I will review them.",
            "thread_id": "thread-1",
            "in_reply_to": "<m1@example.test>",
            "references": "<root@example.test> <m1@example.test>",
        }
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.tool_call_count == 5
        current = await app.services.email.draft(app.principal, draft.id)
        assert current.status.value == "sent"
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run.id, app.principal)
            state = await uow.checkpoints.latest(run.id)
        assert len([item for item in invocations if item.tool_name.endswith("send_message")]) == 1
        assert (
            len([item for item in invocations if item.tool_name.endswith("get_thread_page")]) == 2
        )
        assert state is not None and "mcp.gmail_read.get_profile" not in state.pinned_tool_names


async def test_external_reply_after_approval_invalidates_send_without_dispatch() -> None:
    from dataclasses import replace

    from agent_core.domain.approvals import ApprovalResolutionType
    from tests.gates.test_email_m18 import _email_settings

    factory = await _mailbox_factory(
        [
            ("get_profile", _profile()),
            ("get_thread_page", _page()),
            ("get_profile", _profile()),
            ("get_thread_page", _page(changed=True)),
        ]
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        _, draft = await _seed_draft(app)
        operation = await app.services.email.submit_task(
            app.principal, kind="send", draft_id=draft.id, expected_revision=1
        )
        [approval] = await app.approvals.list_pending(run_id=operation.run_id)
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        current = await app.services.email.draft(app.principal, draft.id)
        assert current.stale
        assert current.status.value != "sent"
        async with app.uow_factory() as uow:
            calls = await uow.invocations.list_for_run(operation.run_id, app.principal)
        assert not any(
            call.effect_sent_at is not None
            for call in calls
            if call.tool_name.endswith("send_message")
        )


async def test_uncertain_send_is_preserved_and_cannot_be_resubmitted() -> None:
    from dataclasses import replace

    import pytest

    from agent_core.domain.approvals import ApprovalResolutionType
    from agent_core.domain.errors import ConflictError
    from tests.gates.test_email_m18 import _email_settings

    factory = await _mailbox_factory(
        [
            ("get_profile", _profile()),
            ("get_thread_page", _page()),
            ("get_profile", _profile()),
            ("get_thread_page", _page()),
        ],
        send_uncertain=True,
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        _, draft = await _seed_draft(app)
        operation = await app.services.email.submit_task(
            app.principal, kind="send", draft_id=draft.id, expected_revision=1
        )
        [approval] = await app.approvals.list_pending(run_id=operation.run_id)
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        current = await app.services.email.draft(app.principal, draft.id)
        assert current.status.value == "uncertain"
        with pytest.raises(ConflictError):
            await app.services.email.submit_task(
                app.principal, kind="send", draft_id=draft.id, expected_revision=1
            )


async def test_failed_freshness_preserves_draft_and_never_dispatches_send() -> None:
    from dataclasses import replace

    from tests.gates.test_email_m18 import _email_settings

    factory = await _mailbox_factory([("get_profile", {"is_error": True})])
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        _, draft = await _seed_draft(app)
        operation = await app.services.email.submit_task(
            app.principal, kind="send", draft_id=draft.id, expected_revision=1
        )
        current = await app.services.email.draft(app.principal, draft.id)
        assert current.status.value == "ready" and current.body == draft.body
        async with app.uow_factory() as uow:
            calls = await uow.invocations.list_for_run(operation.run_id, app.principal)
        assert not any(call.tool_name.endswith("send_message") for call in calls)


async def test_refresh_reserves_history_progress_and_bounds_full_reads_per_account() -> None:
    from dataclasses import replace

    from agent_core.domain.mcp import MCPCallResult
    from agent_core.domain.messages import FakeModelScript
    from tests.gates.test_email_m18 import _email_settings

    base_factory = await _mailbox_factory([])
    calls = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base_factory(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            import json

            calls.append((name, arguments))
            if name == "get_profile":
                value = _profile()
            elif name == "search_threads":
                ids = (
                    [f"inbox-{index}" for index in range(8)]
                    if "in:inbox" in arguments["query"]
                    else ["history-0", "history-1"]
                )
                value = {"threads": [{"thread_id": item} for item in ids]}
            elif name == "sync_changes":
                value = {
                    "schema_version": 1,
                    "history_id": "101",
                    "changes": [
                        {
                            "kind": "message_added",
                            "thread_id": f"change-{index}",
                            "message_id": f"cm-{index}",
                            "label_ids": [],
                        }
                        for index in range(3)
                    ],
                    "next_page_token": None,
                    "resync_required": False,
                }
            else:
                assert name == "get_thread_page"
                value = _page()
                value["thread_id"] = arguments["thread_id"]
                value["messages"][0].update(
                    thread_id=arguments["thread_id"],
                    id=arguments["thread_id"],
                    body_complete=False,
                    body_available=False,
                )
                value["complete"] = False
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(4)]),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        full_reads = [args["thread_id"] for name, args in calls if name == "get_thread_page"]
        assert len(full_reads) <= 10
        assert all(args["max_results"] <= 50 for name, args in calls if name == "sync_changes")
        assert {"history-0", "history-1"} <= set(full_reads)
        async with app.uow_factory() as uow:
            sync = await uow.email.get(app.principal, "sync", "default")
        assert sync is not None
        assert sync.payload["change_pending"] == ["change-0", "change-1", "change-2"]


async def test_changed_mail_is_assessed_and_drafted_once_through_metered_model() -> None:
    import json
    from dataclasses import replace

    from agent_core.domain.mcp import MCPCallResult
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from tests.gates.test_email_m18 import _email_settings

    base_factory = await _mailbox_factory([])

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base_factory(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "get_profile":
                value = _profile()
            elif name == "search_threads":
                value = {
                    "threads": [{"thread_id": "thread-1"}]
                    if "in:inbox" in arguments["query"]
                    else []
                }
            elif name == "get_thread_page":
                value = _page()
            else:
                assert name == "sync_changes"
                value = {
                    "schema_version": 1,
                    "history_id": "100",
                    "changes": [],
                    "resync_required": False,
                    "next_page_token": None,
                }
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                text=json.dumps(
                    {
                        "summary": "Board decision requested",
                        "reason": "A direct request needs a response",
                        "topics": ["board"],
                        "content_importance": 1,
                        "relationship_importance": 0,
                        "urgency": 0,
                        "needs_reply": True,
                        "bulk": False,
                        "supported_evidence": ["Please approve the board materials."],
                    }
                )
            ),
            ScriptedTurn(text='{"body":"Thanks. I will review them."}'),
        ]
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=factory,
    ) as app:
        first = await app.services.email.submit_task(app.principal, kind="refresh")
        first_run = await app.runs.get(first.run_id)
        assert first_run.status is RunStatus.COMPLETED, (
            first_run.failure.message if first_run.failure else None
        )
        assert first_run.model_call_count == 2
        items = _items(await app.services.email.threads(app.principal))
        assert len(items) == 1 and items[0]["draft_id"] is not None
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        second_run = await app.runs.get(second.run_id)
        assert second_run.status is RunStatus.COMPLETED
        assert second_run.model_call_count == 0
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(first_run.session_id, 0, app.principal)
            drafts = await uow.email.list(app.principal, "draft")
        assert len(drafts) == 1
        async with app.uow_factory() as uow:
            semantic_sources = await uow.email.list(app.principal, "semantic_source")
        assert len(semantic_sources) == 1
        assert semantic_sources[0].payload["memory_ids"] == []
        assert len([event for event in events if event.event_type == "model.request.started"]) == 2
        assert not any(event.event_type == "user.message.received" for event in events)


async def _current_mail_factory() -> MailboxFactory:
    import json

    from agent_core.domain.mcp import MCPCallResult

    base = await _mailbox_factory([])

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "get_profile":
                value = _profile()
            elif name == "get_thread_page":
                value = _page()
            elif name == "search_threads":
                value = {
                    "threads": [{"thread_id": "thread-1"}]
                    if "in:inbox" in arguments["query"]
                    else []
                }
            else:
                assert name == "sync_changes"
                value = {
                    "schema_version": 1,
                    "history_id": "100",
                    "changes": [],
                    "resync_required": False,
                    "next_page_token": None,
                }
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


def _assessment_turn(
    *, needs_reply: bool = False, evidence: str = "Please approve the board materials."
) -> ScriptedTurn:
    import json

    from agent_core.domain.messages import ScriptedTurn

    return ScriptedTurn(
        text=json.dumps(
            {
                "summary": "Board request",
                "reason": "Direct request",
                "topics": ["board"],
                "content_importance": 1,
                "relationship_importance": 0,
                "urgency": 0,
                "needs_reply": needs_reply,
                "bulk": False,
                "supported_evidence": [evidence],
            }
        )
    )


async def test_no_reply_feedback_prevents_automatic_draft_after_reassessment() -> None:
    from dataclasses import replace
    from uuid import UUID

    from agent_core.domain.messages import FakeModelScript
    from tests.gates.test_email_m18 import _email_settings

    script = FakeModelScript(turns=[_assessment_turn(), _assessment_turn(needs_reply=True)])
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=await _current_mail_factory(),
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        [thread] = _items(await app.services.email.threads(app.principal))
        await app.services.email.feedback(
            app.principal, thread_id=UUID(thread["id"]), target="thread", judgment="no_reply_needed"
        )
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(second.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "draft") == []


async def test_expired_cached_body_is_refetched_despite_unchanged_provider_cursor() -> None:
    from dataclasses import replace

    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailThread
    from agent_core.domain.messages import FakeModelScript
    from tests.gates.test_email_m18 import _email_settings

    script = FakeModelScript(turns=[_assessment_turn(), _assessment_turn()])
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=await _current_mail_factory(),
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            [row] = await uow.email.list(app.principal, "thread")
            thread = EmailThread.model_validate(row.payload)
            expired = thread.model_copy(
                update={
                    "messages": [
                        message.model_copy(update={"body": "", "complete": False})
                        for message in thread.messages
                    ],
                    "complete": False,
                    "source_fingerprint": "",
                    "assessment_version": "",
                }
            )
            await save_value(
                uow.email, app.principal, "thread", str(thread.id), expired, app.clock.now()
            )
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(second.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "thread")
        refreshed = EmailThread.model_validate(row.payload)
        assert refreshed.complete
        assert refreshed.messages[0].body == "Please approve the board materials."


async def test_deleted_last_message_updates_projection_before_history_cursor_advances() -> None:
    import json
    from dataclasses import replace

    from agent_core.domain.email import EmailThread
    from agent_core.domain.mcp import MCPCallResult
    from agent_core.domain.messages import FakeModelScript
    from tests.gates.test_email_m18 import _email_settings

    base = await _mailbox_factory([])
    generation = 0

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            nonlocal generation
            if name == "get_profile":
                generation += 1
                value = _profile()
            elif name == "search_threads":
                value = {
                    "threads": [{"thread_id": "thread-1"}]
                    if "in:inbox" in arguments["query"] and generation == 1
                    else []
                }
            elif name == "get_thread_page":
                if generation > 1:
                    return MCPCallResult(content=("gmail.provider_rejected",), is_error=True)
                value = _page()
            else:
                assert name == "sync_changes"
                value = {
                    "schema_version": 1,
                    "history_id": "101" if generation > 1 else "100",
                    "changes": [
                        {
                            "kind": "message_deleted",
                            "message_id": "m1",
                            "thread_id": "thread-1",
                            "label_ids": [],
                        }
                    ]
                    if generation > 1
                    else [],
                    "next_page_token": None,
                    "resync_required": False,
                }
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=factory,
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(second.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "thread")
        thread = EmailThread.model_validate(row.payload)
        assert not thread.messages and not thread.in_inbox
        [account] = _items(await app.services.email.accounts(app.principal))
        assert account["history_id"] == "101" and account["status"] == "ready"


async def test_large_body_analysis_advances_in_bounded_passages_without_repeating_reads() -> None:
    import json
    from dataclasses import replace

    from agent_core.domain.mcp import MCPCallResult
    from agent_core.domain.messages import FakeModelScript
    from tests.gates.test_email_m18 import _email_settings

    base = await _current_mail_factory()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        old = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name != "get_thread_page":
                return await old(name, arguments)
            value = _page()
            value["messages"][0]["body"] = "START_SECTION " + "x" * 12000 + " DISTINCT_END_FACT"
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(
            turns=[
                _assessment_turn(evidence="START_SECTION"),
                _assessment_turn(evidence="START_SECTION"),
            ]
        ),
        mcp_client_factory=factory,
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "assessment")
        assert row.payload["analysis_complete"] is False
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        first_request = provider.requests[0].model_dump_json()
        assert "DISTINCT_END_FACT" not in first_request
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(second.run_id)).model_call_count == 1
        second_request = provider.requests[1].model_dump_json()
        assert "DISTINCT_END_FACT" in second_request
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "assessment")
        assert row.payload["analysis_complete"] is True


async def test_body_continuation_keeps_header_provenance_across_refresh_sessions() -> None:
    import json
    from dataclasses import replace

    from agent_core.domain.mcp import MCPCallResult
    from agent_core.domain.messages import FakeModelScript
    from tests.gates.test_email_m18 import _email_settings

    base = await _current_mail_factory()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        old = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "get_thread_page":
                value = _page()
                value["messages"][0].update(
                    body="x" * 65536, body_complete=False, next_body_offset=65536
                )
                value["complete"] = False
            elif name == "get_message_body":
                offset = arguments["offset"]
                value = {
                    "schema_version": 1,
                    "message_id": "m1",
                    "history_id": "100",
                    "body": "x" * (65536 if offset == 65536 else 100),
                    "offset": offset,
                    "next_offset": 131072 if offset == 65536 else None,
                    "complete": offset != 65536,
                    "source_changed": False,
                    "body_available": True,
                }
            else:
                return await old(name, arguments)
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    script = FakeModelScript(
        turns=[_assessment_turn(evidence="xxxx"), _assessment_turn(evidence="xxxx")]
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=factory,
    ) as app:
        first = await app.services.email.submit_task(app.principal, kind="refresh")
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(first.run_id)).status is RunStatus.COMPLETED
        assert (await app.runs.get(second.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "semantic_source")
            [thread] = await uow.email.list(app.principal, "thread")
        passages = row.payload["passages"]
        assert isinstance(passages, dict)
        assert set(passages) == {"0", "65536", "131072"}
        occurrences = row.payload["occurrences"]
        assert isinstance(occurrences, list)
        last = occurrences[-1]
        assert last["session_id"] != last["header_session_id"]
        assert thread.payload["complete"] is True
        assert len(EmailThread.model_validate(thread.payload).messages[0].body) == 131172


async def test_recovery_after_send_dispatch_preserves_uncertainty_before_any_fresh_read() -> None:
    from dataclasses import replace

    from agent_core.domain.tools import ToolInvocationStatus
    from tests.gates.test_email_m18 import _email_settings

    factory = await _mailbox_factory([("get_profile", _profile()), ("get_thread_page", _page())])
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        _, draft = await _seed_draft(app)
        operation = await app.services.email.submit_task(
            app.principal, kind="send", draft_id=draft.id, expected_revision=1
        )
        async with app.uow_factory() as uow:
            calls = await uow.invocations.list_for_run(operation.run_id, app.principal)
            invocation = next(call for call in calls if call.tool_name.endswith("send_message"))
            authorized = invocation.model_copy(update={"status": ToolInvocationStatus.AUTHORIZED})
            await uow.invocations.transition(
                invocation.id, ToolInvocationStatus.WAITING_FOR_APPROVAL, authorized
            )
            running = authorized.model_copy(
                update={"status": ToolInvocationStatus.RUNNING, "effect_sent_at": app.clock.now()}
            )
            await uow.invocations.transition(
                invocation.id, ToolInvocationStatus.AUTHORIZED, running
            )
            run = await uow.runs.get(operation.run_id, app.principal)
            await app.executor.requeue_after_approval(uow, run)
        await app.executor.execute(operation.run_id)
        current = await app.services.email.draft(app.principal, draft.id)
        assert current.status.value == "uncertain"
        async with app.uow_factory() as uow:
            calls = await uow.invocations.list_for_run(operation.run_id, app.principal)
        assert len([call for call in calls if call.tool_name.endswith("get_profile")]) == 1
        assert (await app.runs.get(operation.run_id)).tool_call_count == 3


async def test_editing_pending_send_cancels_old_approval_and_allows_new_exact_proposal() -> None:
    from dataclasses import replace

    import pytest

    from agent_core.domain.approvals import ApprovalResolutionType
    from agent_core.domain.email import EmailDraftEdit
    from agent_core.domain.errors import ConflictError
    from tests.gates.test_email_m18 import _email_settings

    factory = await _mailbox_factory(
        [
            ("get_profile", _profile()),
            ("get_thread_page", _page()),
            ("get_profile", _profile()),
            ("get_thread_page", _page()),
            ("get_profile", _profile()),
            ("get_thread_page", _page()),
        ]
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        _, draft = await _seed_draft(app)
        first = await app.services.email.submit_task(
            app.principal, kind="send", draft_id=draft.id, expected_revision=1
        )
        [old_approval] = await app.approvals.list_pending(run_id=first.run_id)
        revised = await app.services.email.edit_draft(
            app.principal,
            draft.id,
            EmailDraftEdit(
                expected_revision=1,
                source_revision=draft.source_revision,
                to=draft.to,
                cc=draft.cc,
                bcc=draft.bcc,
                subject=draft.subject,
                body="I reviewed the materials. Approved.",
            ),
        )
        assert (await app.runs.get(first.run_id)).status is RunStatus.CANCELLED
        second = await app.services.email.submit_task(
            app.principal, kind="send", draft_id=draft.id, expected_revision=revised.revision
        )
        [new_approval] = await app.approvals.list_pending(run_id=second.run_id)
        assert new_approval.id != old_approval.id
        assert new_approval.arguments["body"] == revised.body
        with pytest.raises(ConflictError):
            await app.approvals.resolve(old_approval.id, ApprovalResolutionType.APPROVE_ONCE)
        await app.approvals.resolve(new_approval.id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.services.email.draft(app.principal, draft.id)).status.value == "sent"
        async with app.uow_factory() as uow:
            calls = [
                *await uow.invocations.list_for_run(first.run_id, app.principal),
                *await uow.invocations.list_for_run(second.run_id, app.principal),
            ]
        assert (
            len(
                [
                    call
                    for call in calls
                    if call.tool_name.endswith("send_message") and call.effect_sent_at is not None
                ]
            )
            == 1
        )


async def test_unsupported_ranking_quotes_cannot_create_attention_or_auto_drafts() -> None:
    from dataclasses import replace

    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from tests.gates.test_email_m18 import _email_settings

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _current_mail_factory(),
        script=FakeModelScript(
            turns=[
                _assessment_turn(
                    needs_reply=True, evidence="Invented emergency absent from this message."
                ),
                ScriptedTurn(text='{"body":"Unrequested draft."}'),
            ]
        ),
    ) as app:
        task = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(task.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
            drafts = await uow.email.list(app.principal, "draft")
        assert EmailThread.model_validate(thread.payload).priority < 0.7
        assert thread.payload["needs_reply"] is False
        assert drafts == []
        assert (await app.runs.get(task.run_id)).model_call_count == 1


async def test_refresh_recovery_accounts_completed_pending_read_before_new_work() -> None:
    from dataclasses import replace
    from uuid import UUID

    import pytest

    from agent_core.domain.messages import FakeModelScript
    from tests.gates.test_email_m18 import _email_settings

    class SimulatedCrash(BaseException):
        pass

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _current_mail_factory(),
        script=FakeModelScript(turns=[_assessment_turn()]),
    ) as app:
        original_dispatch = app.executor._dispatch_tools

        async def crash_after_dispatch(**kwargs: Any) -> list[ToolResultItem]:
            await original_dispatch(**kwargs)
            raise SimulatedCrash()

        app.executor._dispatch_tools = crash_after_dispatch
        with pytest.raises(SimulatedCrash):
            await app.services.email.submit_task(app.principal, kind="refresh")
        app.executor._dispatch_tools = original_dispatch
        async with app.uow_factory() as uow:
            [task] = await uow.email.list(app.principal, "task")
            run = await uow.runs.get(UUID(str(task.payload["run_id"])), app.principal)
        assert run.status is RunStatus.RUNNING
        await app.executor._execute_running(run, lease=None)
        current = await app.runs.get(run.id)
        assert current.status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run.id, app.principal)
            state = await uow.checkpoints.latest(run.id)
        assert current.tool_call_count == len(invocations)
        assert state is not None
        assert all(call["accounted"] for call in state.working_state["email_calls"].values())


async def test_send_recovery_accounts_confirmed_outcome_without_redispatch() -> None:
    from dataclasses import replace

    import pytest

    from agent_core.domain.approvals import ApprovalResolutionType
    from tests.gates.test_email_m18 import _email_settings

    class SimulatedCrash(BaseException):
        pass

    factory = await _mailbox_factory(
        [
            ("get_profile", _profile()),
            ("get_thread_page", _page()),
            ("get_profile", _profile()),
            ("get_thread_page", _page()),
        ]
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        _, draft = await _seed_draft(app)
        task = await app.services.email.submit_task(
            app.principal, kind="send", draft_id=draft.id, expected_revision=1
        )
        [approval] = await app.approvals.list_pending(run_id=task.run_id)
        original_dispatch = app.executor._dispatch_tools

        async def crash_after_send(**kwargs: Any) -> list[ToolResultItem]:
            value = await original_dispatch(**kwargs)
            if kwargs["tool_calls"][0].name.endswith("send_message"):
                raise SimulatedCrash()
            return value

        app.executor._dispatch_tools = crash_after_send
        with pytest.raises(SimulatedCrash):
            await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        app.executor._dispatch_tools = original_dispatch
        run = await app.runs.get(task.run_id)
        await app.executor._execute_running(run, lease=None)
        assert (await app.services.email.draft(app.principal, draft.id)).status.value == "sent"
        assert (await app.runs.get(task.run_id)).tool_call_count == 5
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run.id, app.principal)
        assert len([call for call in invocations if call.tool_name.endswith("send_message")]) == 1


def _items(response: dict[str, object]) -> list[dict[str, Any]]:
    items = response["items"]
    assert isinstance(items, list)
    assert all(isinstance(item, dict) for item in items)
    return items


async def test_rolling_history_waits_for_analysis_then_reaches_later_source_windows(
    monkeypatch: Any,
) -> None:
    import json
    from dataclasses import replace

    from agent_core.domain.messages import FakeModelScript
    from agent_core.runtime import email_tasks
    from tests.gates.test_email_m18 import _email_settings

    monkeypatch.setattr(email_tasks, "EMAIL_MESSAGE_WINDOW", 2, raising=False)
    base = await _current_mail_factory()
    reads: list[int] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        old = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name != "get_thread_page":
                return await old(name, arguments)
            index = int(arguments.get("page_token") or 0)
            reads.append(index)
            page = _page()
            page["messages"][0].update(
                id=f"m{index}", body=f"Passage {index}", internal_date=1789128000000 + index
            )
            page.update(
                total_messages=5,
                next_page_token=str(index + 1) if index < 4 else None,
                complete=index == 4,
            )
            return MCPCallResult(content=(json.dumps(page),), structured=page)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn(evidence="Passage") for _ in range(15)]),
    ) as app:
        for _ in range(3):
            task = await app.services.email.submit_task(app.principal, kind="refresh")
            assert (await app.runs.get(task.run_id)).status is RunStatus.COMPLETED
        assert reads == [0, 1]
        for _ in range(8):
            await app.services.email.submit_task(app.principal, kind="refresh")
        assert reads == [0, 1, 2, 3, 4]
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        contexts = "\n".join(request.model_dump_json() for request in provider.requests)
        assert all(f"Passage {index}" in contexts for index in range(5))
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "thread")
            progress = await uow.email.list(app.principal, "thread_progress")
        assert row.payload["complete"] is False
        assert progress == []


async def test_rolling_body_window_preserves_original_byte_offset_and_final_passage(
    monkeypatch: Any,
) -> None:
    import json
    from dataclasses import replace

    from agent_core.domain.messages import FakeModelScript
    from agent_core.runtime import email_tasks
    from tests.gates.test_email_m18 import _email_settings

    monkeypatch.setattr(email_tasks, "EMAIL_BODY_WINDOW_BYTES", 80_000)
    base = await _current_mail_factory()
    offsets: list[int] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        old = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "get_thread_page":
                value = _page()
                value["messages"][0].update(
                    body="A" * 65536, body_complete=False, next_body_offset=65536
                )
                value["complete"] = False
            elif name == "get_message_body":
                offset = arguments["offset"]
                offsets.append(offset)
                value = {
                    "schema_version": 1,
                    "message_id": "m1",
                    "history_id": "100",
                    "body": "B" * 65536 if offset == 65536 else "FINAL_PASSAGE",
                    "offset": offset,
                    "next_offset": 131072 if offset == 65536 else None,
                    "complete": offset != 65536,
                    "source_changed": False,
                    "body_available": True,
                }
            else:
                return await old(name, arguments)
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(
            turns=[
                *[_assessment_turn(evidence="A") for _ in range(16)],
                _assessment_turn(evidence="FINAL_PASSAGE"),
            ]
        ),
    ) as app:
        for _ in range(18):
            task = await app.services.email.submit_task(app.principal, kind="refresh")
            current = await app.runs.get(task.run_id)
            assert current.status is RunStatus.COMPLETED, current.failure
        assert offsets == [65536, 131072]
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "thread")
            [source] = await uow.email.list(app.principal, "semantic_source")
        thread = EmailThread.model_validate(row.payload)
        assert thread.complete is False
        assert thread.messages[0].body_offset == 131072
        assert thread.messages[0].body == "FINAL_PASSAGE"
        assert thread.messages[0].complete is False
        passages = source.payload["passages"]
        assert isinstance(passages, dict) and set(passages) == {"0", "65536", "131072"}
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        assert "FINAL_PASSAGE" in provider.requests[-1].model_dump_json()


async def test_partial_large_thread_does_not_block_other_current_mail() -> None:
    import json
    from dataclasses import replace

    from agent_core.domain.messages import FakeModelScript
    from tests.gates.test_email_m18 import _email_settings

    base = await _current_mail_factory()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        old = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            value: dict[str, Any]
            if name == "search_threads" and "in:inbox" in arguments["query"]:
                value = {"threads": [{"thread_id": "thread-1"}, {"thread_id": "important-2"}]}
            elif name == "get_thread_page":
                value = _page()
                value["thread_id"] = arguments["thread_id"]
                value["messages"][0].update(
                    thread_id=arguments["thread_id"], id=arguments["thread_id"]
                )
                if arguments["thread_id"] == "thread-1":
                    value.update(next_page_token="later-page", complete=False)
            else:
                return await old(name, arguments)
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn(), _assessment_turn()]),
    ) as app:
        task = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(task.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            threads = await uow.email.list(app.principal, "thread")
        assert {row.payload["provider_thread_id"] for row in threads} == {"thread-1", "important-2"}


@pytest.mark.parametrize("kind", ["refresh", "send"])
async def test_invalid_json_mailbox_result_preserves_account_and_draft(
    kind: Literal["refresh", "send"],
) -> None:
    from dataclasses import replace

    from tests.gates.test_email_m18 import _email_settings

    base_factory = await _mailbox_factory([])

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base_factory(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            return MCPCallResult(content=("{malformed private mailbox response",))

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        draft = (await _seed_draft(app))[1] if kind == "send" else None
        operation = await app.services.email.submit_task(
            app.principal,
            kind=kind,
            draft_id=draft.id if draft is not None else None,
            expected_revision=1 if kind == "send" else None,
        )
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED, (
            await app.runs.get(operation.run_id)
        ).model_dump()
        if draft is not None:
            current = await app.services.email.draft(app.principal, draft.id)
            assert current.status.value == "ready" and current.body == draft.body
        if kind == "refresh":
            account = _items(await app.services.email.accounts(app.principal))[0]
            assert account["status"] == "unavailable"


@pytest.mark.parametrize("missing", ["id", "from", "internal_date"])
async def test_invalid_memory_source_fields_do_not_interrupt_mailbox_viewing(missing: str) -> None:
    from types import SimpleNamespace
    from typing import cast
    from unittest.mock import AsyncMock
    from uuid import UUID

    from agent_core.runtime.email_tasks import _TaskIO

    message = dict(_page()["messages"][0])
    del message[missing]
    semantics = SimpleNamespace(register_source=AsyncMock())
    io = cast(
        _TaskIO,
        SimpleNamespace(
            context=SimpleNamespace(run=SimpleNamespace(session_id=UUID(int=1)), lease=None),
            semantics=semantics,
        ),
    )
    await _TaskIO._register_sources(
        io,
        "work",
        {
            "thread_id": "t1",
            "source_event_sequence": 1,
            "source_tool_name": "mcp.gmail_read.get_thread_page",
            "messages": [message],
        },
    )
    semantics.register_source.assert_not_awaited()
