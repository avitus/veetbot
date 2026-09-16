"""An owner's archive gesture never waits behind queued asynchronous email work."""

import json
from dataclasses import replace
from typing import Any, cast

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _current_mail_factory, _seed_draft
from tests.integration.m2_support import database_settings


async def test_archive_is_claimed_by_the_interactive_lane_ahead_of_a_queued_refresh() -> None:
    """Production runs one worker per lane; a refresh must not hold the owner's archive."""
    base = await _current_mail_factory()
    writes: list[dict[str, Any]] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Answer the archive write deterministically without a real mailbox."""
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Record the sole provider mutation and return its receipt."""
            if name == "modify_labels":
                writes.append(arguments)
                receipt = {key: value or [] for key, value in arguments.items()}
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
        thread, _draft = await _seed_draft(app)
        refresh = await app.services.email.submit_task(
            app.principal, kind="refresh", idempotency_key="queued-refresh"
        )
        archive = await app.services.email.archive(
            app.principal,
            thread.id,
            thread.revision,
            archived=True,
            idempotency_key="owner-archive",
        )
        assert refresh.status == archive.status == RunStatus.QUEUED.value

        interactive = cast(DurableWorker, app.worker_factory("interactive-lane"))
        assert await interactive.run_once()
        completed = await app.runs.get(archive.run_id)
        assert completed.status is RunStatus.COMPLETED, completed.failure
        assert (await app.runs.get(refresh.run_id)).status is RunStatus.QUEUED
        assert writes == [
            {"thread_ids": ["thread-1"], "add_label_ids": None, "remove_label_ids": ["INBOX"]}
        ]
        # Refresh stays asynchronous: the interactive lane has nothing else to claim.
        assert not await interactive.run_once()
