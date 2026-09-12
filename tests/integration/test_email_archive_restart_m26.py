"""A queued Gmail archive survives real PostgreSQL and worker composition restarts."""

import json
from dataclasses import replace
from typing import Any

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _current_mail_factory, _seed_draft
from tests.integration.m2_support import database_settings


async def test_archive_consent_and_result_survive_postgres_worker_restart() -> None:
    """Persist exact consent, resolve once under a lease, and replay without another write."""
    base = await _current_mail_factory()
    writes: list[tuple[str, dict[str, Any]]] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Use the real tool pipeline with deterministic, isolated Gmail responses."""
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Record the sole provider mutation without contacting a real mailbox."""
            if name == "modify_labels":
                writes.append((config.server_id, arguments))
                receipt = {
                    "thread_ids": arguments["thread_ids"],
                    "add_label_ids": arguments.get("add_label_ids") or [],
                    "remove_label_ids": arguments.get("remove_label_ids") or [],
                }
                return MCPCallResult(content=(json.dumps(receipt),), structured=receipt)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    settings = replace(
        _email_settings(),
        database_url=database_settings().database_url,
        email_mode_enabled=True,
    )
    async with build(settings=settings, storage="postgres", mcp_client_factory=factory) as app:
        thread, draft = await _seed_draft(app)
        operation = await app.services.email.archive(
            app.principal,
            thread.id,
            thread.revision,
            archived=True,
            idempotency_key="durable-archive",
        )
        assert operation.status == RunStatus.QUEUED.value
        assert writes == []
        pending = await app.services.email.thread(app.principal, thread.id)
        assert pending["in_inbox"] is True
        pending_operation = pending["archive_operation"]
        assert isinstance(pending_operation, dict) and pending_operation["status"] == "pending"

    async with build(settings=settings, storage="postgres", mcp_client_factory=factory) as app:
        task = await app.services.email.get_task(app.principal, operation.run_id)
        assert task is not None and task.archive_consent is not None
        assert task.archive_consent.provider_thread_id == "thread-1"
        assert task.archive_consent.write_server_id == "gmail_write"
        worker = DurableWorker(
            uow_factory=app.uow_factory,
            executor=app.executor,
            clock=app.clock,
            worker_id="archive-after-restart",
        )
        assert await worker.run_once()
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.model_call_count == 0 and run.usage.cost == 0
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run.id, app.principal)
            [write] = [item for item in invocations if item.tool_name.endswith("modify_labels")]
            approval = await uow.approvals.get_by_action(write.id)
            assert approval is not None and approval.status is ApprovalStatus.APPROVED
            assert approval.resolved_by == app.principal.principal_id
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        requested = [event for event in events if event.event_type == "approval.requested"]
        resolved = [event for event in events if event.event_type == "approval.resolved"]
        assert len(requested) == len(resolved) == 1
        assert requested[0].sequence < resolved[0].sequence

    async with build(settings=settings, storage="postgres", mcp_client_factory=factory) as app:
        completed = await app.services.email.thread(app.principal, thread.id)
        assert completed["in_inbox"] is False
        assert completed["revision"] == thread.revision
        completed_operation = completed["archive_operation"]
        assert isinstance(completed_operation, dict)
        assert completed_operation["status"] == "completed"
        preserved = await app.services.email.draft(app.principal, draft.id)
        assert preserved.body == draft.body and not preserved.stale
        replay = await app.services.email.archive(
            app.principal,
            thread.id,
            thread.revision,
            archived=True,
            idempotency_key="durable-archive",
        )
        assert replay.replayed and replay.run_id == operation.run_id
    assert writes == [
        (
            "gmail_write",
            {"thread_ids": ["thread-1"], "add_label_ids": None, "remove_label_ids": ["INBOX"]},
        )
    ]
