"""An Email refresh gives observed correspondence a short summary (ADR-0126)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.people import PeopleInteraction, PeopleQuery
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _mailbox_factory, _profile

BODY = "Can you send me the board deck before Thursday's call?"


def _sent_page() -> dict[str, Any]:
    sent_at = (datetime.now(UTC) - timedelta(days=2)).replace(microsecond=0)
    message = {
        "id": "m-sent",
        "thread_id": "thread-sent",
        "from": "Owner <owner@example.test>",
        "to": "Alex Rivera <alex@example.test>",
        "subject": "Board deck",
        "date": format_datetime(sent_at),
        "internal_date": int(sent_at.timestamp() * 1000),
        "history_id": "100",
        "body": BODY,
        "body_complete": True,
        "headers_complete": True,
        "body_available": True,
        "label_ids": ["INBOX", "SENT"],
        "direction": "sent",
        "message_id_header": "<m-sent@example.test>",
        "references": "",
        "attachments": [],
    }
    return {
        "schema_version": 1,
        "thread_id": "thread-sent",
        "history_id": "100",
        "total_messages": 1,
        "returned_messages": 1,
        "messages": [message],
        "next_page_token": None,
        "complete": True,
        "source_changed": False,
    }


async def _mailbox() -> Any:
    base = await _mailbox_factory([])

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> Any:
        client = base(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "get_profile":
                value: dict[str, Any] = _profile()
            elif name == "get_thread_page":
                value = _sent_page()
            elif name == "search_threads":
                inbox = "in:inbox" in arguments["query"]
                thread = {"thread_id": "thread-sent", "label_ids": ["INBOX", "SENT"]}
                value = {"threads": [thread] if inbox else []}
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


def _assessment() -> ScriptedTurn:
    return ScriptedTurn(
        text=json.dumps(
            {
                "summary": "You asked Alex for the board deck.",
                "reason": "Your own request",
                "topics": ["board"],
                "content_importance": 0.3,
                "relationship_importance": 0,
                "urgency": 0,
                "needs_reply": False,
                "bulk": False,
                "attention_expires_at": None,
                "reply_blocked_reason": None,
                "supported_evidence": ["send me the board deck"],
                "relationship_memory_ids": [],
                "semantic_facts": [],
                "people_facts": [],
            }
        )
    )


def _summary(evidence: str) -> ScriptedTurn:
    return ScriptedTurn(
        text=json.dumps(
            {
                "summary": "Asked Alex for the board deck before Thursday's call.",
                "evidence": evidence,
            }
        )
    )


async def _refresh(*turns: ScriptedTurn) -> tuple[Any, list[PeopleInteraction]]:
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=list(turns)),
        mcp_client_factory=await _mailbox(),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        async with app.uow_factory() as uow:
            rows = await uow.people.query(
                PeopleQuery(
                    tenant_id=app.principal.tenant_id,
                    principal_id=app.principal.principal_id,
                    kinds=["interaction"],
                    sensitivity_ceiling=Sensitivity.RESTRICTED,
                    limit=100,
                )
            )
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        summarized = [e for e in events if e.event_type == "email.correspondence.summarized"]
        return (run, summarized), [row for row in rows if isinstance(row, PeopleInteraction)]


async def test_refresh_summarizes_the_exchange_after_assessing_it() -> None:
    (run, summarized), [interaction] = await _refresh(
        _assessment(), _summary("send me the board deck")
    )

    assert run.model_call_count == 2
    assert (
        interaction.summary == "Sent email: Asked Alex for the board deck before Thursday's call."
    )
    assert interaction.summary_provenance is not None
    assert interaction.summary_provenance.state == "generated"
    assert interaction.summary_provenance.model is not None
    # The audit event names the record and the outcome, never the mail itself.
    [event] = summarized
    assert event.payload["state"] == "generated"
    assert BODY not in json.dumps(event.payload)


async def test_an_ungrounded_summary_is_not_kept_and_waits_for_one_retry() -> None:
    (run, summarized), [interaction] = await _refresh(
        _assessment(), _summary("words the message never used")
    )

    assert run.model_call_count == 2
    assert interaction.summary == "Sent email"
    assert interaction.summary_provenance is not None
    assert interaction.summary_provenance.state == "retry"
    assert [event.payload["state"] for event in summarized] == ["retry"]


async def test_a_failed_summary_call_ends_the_stage_but_not_the_refresh() -> None:
    from uuid import UUID

    from agent_core.domain.messages import ModelPermanentError

    failed = ScriptedTurn(
        fail_with=ModelPermanentError(
            provider="fake",
            model="scripted",
            attempt_id=UUID(int=0),
            message="The model provider failed.",
            http_status=500,
        )
    )

    (run, summarized), [interaction] = await _refresh(_assessment(), failed)

    assert run.model_call_count == 2
    assert interaction.summary == "Sent email"
    # Nothing was recorded, so the next refresh tries this exchange again.
    assert interaction.summary_provenance is None
    assert summarized == []
