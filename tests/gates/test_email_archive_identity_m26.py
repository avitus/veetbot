"""Adversarial account and frozen-consent checks for explicit Gmail archive actions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient, ScriptedMCPClientFactory
from agent_core.application.email import save_value
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings, load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EmailAccount, EmailThread
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig, ScriptedMCPServer
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import Run, RunStatus
from tests.gates.test_email_archive_m26 import _archive_factory, _archive_status
from tests.gates.test_email_m18 import (
    _accounts_manifest,
    _base_environment,
    _email_settings,
    _generated_gmail_discovery,
)
from tests.gates.test_email_runtime_m26 import MailboxFactory, _page, _profile, _seed_draft


def _named_settings(tmp_path: Path) -> Settings:
    """Reuse the real named-account manifest and credential loader with fake credentials."""
    return replace(
        load_settings(
            {
                **_base_environment(),
                "AGENT_EMAIL_ENABLED": "1",
                "GMAIL_ACCOUNTS_FILE": str(_accounts_manifest(tmp_path)),
            }
        ),
        email_mode_enabled=True,
    )


def _named_page(account: str) -> dict[str, Any]:
    """Use overlapping provider IDs but distinct mailbox ownership in both accounts."""
    page = _page()
    page["messages"][0]["to"] = f"owner@{account}.example.test"
    return page


async def _named_factory(writes: list[tuple[str, dict[str, Any]]]) -> MailboxFactory:
    """Expose real Gmail schemas while recording which account-bound server receives a write."""
    schemas = {mode: await _generated_gmail_discovery(mode) for mode in ("read", "write", "send")}
    base = ScriptedMCPClientFactory(
        {
            server: ScriptedMCPServer(name=server, discovery=schemas[mode])
            for prefix in ("gmail", "gmail_work")
            for mode in schemas
            for server in (f"{prefix}_{mode}",)
        }
    )

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Bind mailbox identity to the configured server, never to caller-supplied arguments."""
        client = base(config, credential, environment)
        account = "work" if config.server_id.startswith("gmail_work_") else "personal"

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Return bounded profile/source data or an exact receipt from the selected mailbox."""
            if name == "modify_labels":
                writes.append((config.server_id, arguments))
                value = {key: value or [] for key, value in arguments.items()}
            elif name == "get_profile":
                value = {
                    **_profile(),
                    "email_address": f"owner@{account}.example.test",
                    "verified_addresses": [f"owner@{account}.example.test"],
                }
            else:
                assert name == "get_thread_page"
                value = _named_page(account)
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


async def _seed_named(app: Composition, account: str) -> EmailThread:
    """Persist verified mailbox identity and source through the real email service."""
    async with app.uow_factory() as uow, uow.email.lock(app.principal):
        await save_value(
            uow.email,
            app.principal,
            "account",
            account,
            EmailAccount(
                id=account,
                label=account.title(),
                email_address=f"owner@{account}.example.test",
                verified_addresses=[f"owner@{account}.example.test"],
            ),
            app.clock.now(),
        )
    session = await app.sessions.create()
    return await app.services.email.import_thread(
        app.principal, account, _named_page(account), session
    )


async def test_archive_routes_overlapping_provider_ids_to_each_originating_account(
    tmp_path: Path,
) -> None:
    """Identical Gmail thread IDs in two mailboxes never collapse or select the default account."""
    writes: list[tuple[str, dict[str, Any]]] = []
    async with build(
        settings=_named_settings(tmp_path), mcp_client_factory=await _named_factory(writes)
    ) as app:
        threads = [await _seed_named(app, account) for account in ("personal", "work")]
        assert threads[0].id != threads[1].id
        for thread in threads:
            operation = await app.services.email.archive(
                app.principal,
                thread.id,
                thread.revision,
                archived=True,
                idempotency_key=f"archive-{thread.account_id}",
            )
            run = await app.runs.get(operation.run_id)
            assert run.status is RunStatus.COMPLETED, run.failure
            assert run.model_call_count == 0
            assert (
                _archive_status(await app.services.email.thread(app.principal, thread.id))
                == "completed"
            )
        assert writes == [
            (
                server,
                {"thread_ids": ["thread-1"], "add_label_ids": None, "remove_label_ids": ["INBOX"]},
            )
            for server in ("gmail_write", "gmail_work_write")
        ]


@pytest.mark.parametrize("account", ["personal", "work"])
async def test_archive_pinned_binding_rejects_default_account_change(
    tmp_path: Path, account: str
) -> None:
    """A changed default-account map cannot redirect a gesture admitted under the old binding."""
    writes: list[tuple[str, dict[str, Any]]] = []
    async with build(
        settings=_named_settings(tmp_path), mcp_client_factory=await _named_factory(writes)
    ) as app:
        thread = await _seed_named(app, account)
        dispatch = app.services.email.dispatch

        async def queued(run_id: UUID) -> None:
            """Hold the admitted task until the configured account map changes."""

        app.services.email.dispatch = queued
        operation = await app.services.email.archive(
            app.principal,
            thread.id,
            thread.revision,
            archived=True,
            idempotency_key="before-default-change",
        )
        previous = app.services.email.account_servers
        app.services.email.account_servers = {
            "personal": previous["work"],
            "work": previous["personal"],
        }
        await dispatch(operation.run_id)
        assert writes == []
        app.services.email.account_servers = previous
        latest = await app.services.email.thread(app.principal, thread.id)
        assert latest["in_inbox"] is True
        assert _archive_status(latest) == "failed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "another-tenant"),
        ("principal_id", "another-owner"),
        ("account_id", "another-account"),
        ("provider_thread_id", "another-thread"),
        ("read_server_id", "gmail_work_read"),
        ("write_server_id", "gmail_work_write"),
        ("archived", False),
    ],
)
async def test_archive_rejects_tampered_durable_consent(field: str, value: object) -> None:
    """Stored consent must still match the authenticated gesture, source and frozen tool binding."""
    writes: list[dict[str, Any]] = []
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _archive_factory(writes),
    ) as app:
        thread, _ = await _seed_draft(app)
        dispatch = app.services.email.dispatch

        async def queued(run_id: UUID) -> None:
            """Expose the durable admission boundary before worker execution."""

        app.services.email.dispatch = queued
        operation = await app.services.email.archive(
            app.principal,
            thread.id,
            1,
            archived=True,
            idempotency_key="tampered-consent",
        )
        task = await app.services.email.get_task(app.principal, operation.run_id)
        assert task is not None and task.archive_consent is not None
        changed = task.model_copy(
            update={"archive_consent": task.archive_consent.model_copy(update={field: value})}
        )
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await save_value(
                uow.email, app.principal, "task", str(operation.run_id), changed, app.clock.now()
            )
        await dispatch(operation.run_id)
        assert writes == []
        assert await app.approvals.list_pending(run_id=operation.run_id) == []
        latest = await app.services.email.thread(app.principal, thread.id)
        assert latest["in_inbox"] is True
        assert _archive_status(latest) == "failed"


@pytest.mark.parametrize("mutation", ["tool", "arguments", "hash", "run"])
async def test_archive_refuses_an_approval_for_a_different_frozen_action(mutation: str) -> None:
    """Consent consumption rejects a substituted approval even when the surrounding run is valid."""
    writes: list[dict[str, Any]] = []
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _archive_factory(writes),
    ) as app:
        thread, _ = await _seed_draft(app)
        approve = app.services.email.approve_archive

        async def substitute(
            owner: Principal, run: Run, lease: WorkerLease | None, approval_id: UUID
        ) -> None:
            """Substitute a pending test record before its exact-action comparison."""
            async with app.uow_factory() as uow:
                original = await uow.approvals.get(approval_id, owner)
                variants: dict[str, dict[str, object]] = {
                    "tool": {"tool_name": "mcp.gmail_work_write.modify_labels"},
                    "arguments": {"arguments": {**original.arguments, "add_label_ids": ["TRASH"]}},
                    "hash": {"normalized_arguments_hash": "different-action"},
                    "run": {"run_id": uuid4()},
                }
                changes = variants[mutation]
                await uow.approvals.discard_pending(approval_id)
                await uow.approvals.create(original.model_copy(update=changes))
            await approve(owner, run, lease, approval_id)

        vars(app.services.email)["approve_archive"] = substitute
        operation = await app.services.email.archive(
            app.principal,
            thread.id,
            1,
            archived=True,
            idempotency_key=f"different-{mutation}",
        )
        assert writes == []
        assert (await app.runs.get(operation.run_id)).model_call_count == 0
        latest = await app.services.email.thread(app.principal, thread.id)
        assert latest["in_inbox"] is True
        assert _archive_status(latest) == "failed"
