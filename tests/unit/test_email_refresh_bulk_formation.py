"""The email refresh forms nothing from bulk mail (ADR-0116).

Two signals gate formation in the refresh: the unsubscribe census, known before
the model is called, and the assessment's ``bulk`` verdict, known after. Each
leaves a content-free ``email.semantic.skipped`` event naming its reason so the
absence of a memory can be explained.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.contract.memory_fixtures import browse_query
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _mailbox_factory, _page, _profile

EVIDENCE = "Please approve the board materials."


def _people_assessment(*, bulk: bool) -> ScriptedTurn:
    """One grounded People-enabled assessment carrying a single attributed fact."""
    return ScriptedTurn(
        text=json.dumps(
            {
                "summary": "Board request",
                "reason": "Direct request",
                "topics": ["board"],
                "content_importance": 0.6,
                "relationship_importance": 0,
                "urgency": 0,
                "needs_reply": False,
                "bulk": bulk,
                "attention_expires_at": None,
                "reply_blocked_reason": None,
                "supported_evidence": [EVIDENCE],
                "relationship_memory_ids": [],
                "semantic_facts": [],
                "people_facts": [
                    {
                        "message_id": "m1",
                        "quote": EVIDENCE,
                        "belief_type": "fact",
                        "subject": "Board materials",
                        "predicate": "requested_action",
                        "value": "approve the board materials",
                        "confidence": 0.4,
                        "people": None,
                    }
                ],
            }
        )
    )


async def _mail(*, census: bool) -> Any:
    """The current-mail mailbox, optionally announcing thread-1 as one-click bulk mail."""
    base = await _mailbox_factory([])

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> Any:
        client = base(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "get_profile":
                value: dict[str, Any] = _profile()
            elif name == "get_thread_page":
                value = _page()
            elif name == "search_threads":
                summary: dict[str, Any] = {"thread_id": "thread-1", "label_ids": ["INBOX"]}
                if census:
                    summary["bulk"] = {
                        "message_id": "m1",
                        "from": "Colleague <colleague@example.test>",
                        "date": format_datetime(datetime.now(UTC) - timedelta(days=1)),
                        "list_id": "digest.example.test",
                        "unsubscribe": "one_click",
                    }
                value = {"threads": [summary] if "in:inbox" in arguments["query"] else []}
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


@pytest.mark.parametrize(
    ("census", "bulk", "expected_memories", "reason"),
    [
        (False, False, 1, None),
        (False, True, 0, "bulk_assessment"),
        (True, False, 0, "bulk_sender"),
    ],
)
async def test_refresh_forms_from_ordinary_mail_and_skips_bulk_with_a_reason(
    census: bool, bulk: bool, expected_memories: int, reason: str | None
) -> None:
    settings = replace(
        _email_settings(),
        email_mode_enabled=True,
        people_enabled=True,
        email_unsubscribe_enabled=census,
    )
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_people_assessment(bulk=bulk)]),
        mcp_client_factory=await _mail(census=census),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.model_call_count == 1

        async with app.uow_factory() as uow:
            memories = await uow.memories.browse(
                browse_query(
                    tenant_id=app.principal.tenant_id, principal_id=app.principal.principal_id
                )
            )
            sources = await uow.email.list(app.principal, "semantic_source")
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        assert len(memories) == expected_memories
        skipped = [event for event in events if event.event_type == "email.semantic.skipped"]
        if reason is None:
            assert skipped == []
            assert len(sources) == 1
            memory_ids = sources[0].payload["memory_ids"]
            assert isinstance(memory_ids, list) and len(memory_ids) == 1
        else:
            assert [event.payload["reason"] for event in skipped] == [reason]
            assert set(skipped[0].payload) == {"account_id", "thread_id", "reason", "facts"}
            assert skipped[0].payload["facts"] == 1
        if census:
            # Bulk mail never registers a source, so nothing remains for a later import.
            assert sources == []
