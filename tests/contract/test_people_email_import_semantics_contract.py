"""Behavioral contract for the people email import semantics composition interface."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from agent_core.adapters.determinism import SystemClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.application.people import PublicPeopleService
from agent_core.bootstrap import build
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import (
    FakeModelScript,
    ModelUsage,
    ScriptedTurn,
)
from agent_core.domain.people_imports import PeopleImportRequest
from tests.contract.support import principal
from tests.integration.m2_support import memory_settings


@pytest.mark.parametrize(
    "budget_pause,excluded,passages,malformed",
    [
        (False, False, 1, None),
        (True, False, 1, None),
        (False, True, 1, None),
        (False, False, 4, None),
        (False, False, 1, "invalid"),
        (False, False, 1, "naive"),
        (False, False, 1, "missing"),
        (False, False, 1, "reader"),
    ],
)
async def test_email_import_uses_original_date_one_assessment_and_shared_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    budget_pause: bool,
    excluded: bool,
    passages: int,
    malformed: str | None,
) -> None:
    import json
    from uuid import uuid4

    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailAccount, EmailBudgetLimits
    from agent_core.domain.email_semantics import EmailSemanticSource
    from agent_core.domain.messages import TextPart, ToolResultItem
    from agent_core.domain.policies import TrustLevel
    from agent_core.memory.email_people import EmailPeopleFormationService
    from agent_core.memory.people_formation import email_source_id
    from tests.gates.test_email_runtime_m26 import _assessment_turn

    monkeypatch.setattr(EmailPeopleFormationService, "enabled", property(lambda self: True))
    monkeypatch.setattr(
        "agent_core.memory.email_people_evidence.load_email_people_evidence", lambda *a, **kw: None
    )
    evidence = tmp_path / "synthetic-evaluated-fixture.json"
    evidence.write_text('{"policy_version":"email-semantic@2"}')
    owner = principal().model_copy(
        update={
            "scopes": {"people.read", "people.write", "session.read", "session.write", "email.read"}
        }
    )
    scripted = _assessment_turn(evidence="Alex prefers tea.")
    value = json.loads(scripted.text)
    value["people_facts"] = [
        {
            "message_id": "m1",
            "quote": "Alex prefers tea.",
            "belief_type": "preference",
            "subject": "Alex preference",
            "predicate": "prefers",
            "value": "tea",
            "people": None,
        }
    ]
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(text=json.dumps(value), usage=ModelUsage(cost=Decimal("0.01")))
                for _ in range(passages)
            ]
        ),
        SystemClock(),
    )
    async with build(
        settings=replace(memory_settings(), people_enabled=True, email_semantic_evidence=evidence),
        storage="memory",
        principal=owner,
        memory_people_evaluation_mode=True,
        model_provider_overrides={"fake": provider},
    ) as app:
        people = app.services.people
        assert isinstance(people, PublicPeopleService)
        assert people.imports.email_capture_available
        if budget_pause:
            app.services.email.budget_limits = EmailBudgetLimits(
                daily_cost=Decimal("0.01"), monthly_cost=Decimal("0.01")
            )
            monkeypatch.setattr(
                "agent_core.runtime.people_imports.reservation_cost", lambda *a: Decimal("0.02")
            )
        audit = await app.services.sessions.create(owner, "general", {})
        async with app.uow_factory() as uow:
            original = await uow.sessions.get(audit.id, owner)
            source_session = original.model_copy(
                update={
                    "id": uuid4(),
                    "metadata": {"email_account_servers": {"work": {"read": "gmail_work_read"}}},
                }
            )
            await uow.sessions.create(source_session)
        sent_at = (app.clock.now() - timedelta(days=200)).replace(microsecond=0)
        async with app.uow_factory() as uow:
            await save_value(
                uow.email,
                owner,
                "account",
                "work",
                EmailAccount(
                    id="work", label="Work", status="ready", email_address="owner@example.test"
                ),
                app.clock.now(),
            )
            event = await uow.events.append(
                NewEvent(
                    session_id=source_session.id,
                    run_id=None,
                    event_type="tool.call.completed",
                    actor_type="runtime",
                    payload={
                        "name": "mcp.gmail_work_read.get_thread_page",
                        "result_item": ToolResultItem(
                            call_id="read-1",
                            trust=TrustLevel.EXTERNAL_UNTRUSTED,
                            content=[
                                TextPart(
                                    text=json.dumps(
                                        {
                                            "thread_id": "t1",
                                            "messages": [
                                                {
                                                    "id": "m1",
                                                    "from": "Alex <alex@example.test>",
                                                    "body": "Alex prefers tea.",
                                                    "headers_complete": True,
                                                    "body_complete": passages == 1,
                                                    "body_available": True,
                                                    "next_body_offset": len(b"Alex prefers tea.")
                                                    if passages > 1
                                                    else None,
                                                    "history_id": "h1",
                                                    "internal_date": str(
                                                        int(sent_at.timestamp() * 1000)
                                                    ),
                                                }
                                            ],
                                        }
                                    )
                                )
                            ],
                        ).model_dump(mode="json"),
                    },
                )
            )
        source = EmailSemanticSource(
            account_id="work",
            provider_thread_id="t1",
            message_id="m1",
            session_id=source_session.id,
            source_event_sequence=event.sequence,
            tool_name="mcp.gmail_work_read.get_thread_page",
            sender="Alex <alex@example.test>",
            body="Alex prefers tea.",
            sent_at=sent_at,
        )
        from agent_core.ports.people_runtime import PeopleEmailImportSemantics

        semantics: PeopleEmailImportSemantics = EmailPeopleFormationService(
            app.uow_factory, app.clock, app.ids, owner, provider="fake", model="scripted"
        )
        await semantics.register_source(source)
        for index in range(1, passages):
            offset = len(source.body.encode()) * index
            async with app.uow_factory() as uow:
                chunk = await uow.events.append(
                    NewEvent(
                        session_id=source.session_id,
                        run_id=None,
                        event_type="tool.call.completed",
                        actor_type="runtime",
                        payload={
                            "name": "mcp.gmail_work_read.get_message_body",
                            "result_item": ToolResultItem(
                                call_id=f"chunk-{index}",
                                trust=TrustLevel.EXTERNAL_UNTRUSTED,
                                content=[
                                    TextPart(
                                        text=json.dumps(
                                            {
                                                "message_id": "m1",
                                                "history_id": "h1",
                                                "offset": offset,
                                                "body": source.body,
                                                "source_changed": False,
                                                "body_available": True,
                                                "complete": index == passages - 1,
                                                "next_offset": None
                                                if index == passages - 1
                                                else offset + len(source.body.encode()),
                                            }
                                        )
                                    )
                                ],
                            ).model_dump(mode="json"),
                        },
                    )
                )
            await semantics.register_source(
                source.model_copy(
                    update={
                        "source_event_sequence": chunk.sequence,
                        "header_event_sequence": event.sequence,
                        "tool_name": "mcp.gmail_work_read.get_message_body",
                        "body_offset": offset,
                    }
                )
            )
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": str(source_session.id),
                "scope": {
                    "account_ids": ["work"],
                    "since": (sent_at - timedelta(days=1)).isoformat(),
                    "until": (sent_at + timedelta(days=1)).isoformat(),
                    "max_records": 10,
                    "excluded_source_ids": [str(email_source_id(owner, source))]
                    if excluded
                    else [],
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await people.create_import(
            owner, request, key="email-preview", ceiling=Sensitivity.SENSITIVE
        )
        if malformed:
            from typing import Any

            from agent_core.adapters.persistence.email import InMemoryEmailStore
            from agent_core.domain.email import EmailRecord

            original_window = InMemoryEmailStore.list_semantic_window

            async def malformed_page(
                store: InMemoryEmailStore, *args: Any, **kwargs: Any
            ) -> list[EmailRecord]:
                if malformed == "reader":
                    raise ValueError("invalid retained timestamp")
                rows = await original_window(store, *args, **kwargs)
                for row in rows:
                    if malformed == "missing":
                        row.payload.pop("evidence_at")
                    else:
                        row.payload["evidence_at"] = (
                            "2026-01-01T00:00:00" if malformed == "naive" else "invalid"
                        )
                return rows

            monkeypatch.setattr(InMemoryEmailStore, "list_semantic_window", malformed_page)
        result = await asyncio.wait_for(
            people.create_import(
                owner,
                request.model_copy(
                    update={
                        "phase": "apply",
                        "operation_id": preview.id,
                        "expected_revision": preview.revision,
                    }
                ),
                key="email-apply",
                ceiling=Sensitivity.SENSITIVE,
            ),
            timeout=5,
        )
        if malformed:
            assert result.state == "failed"
            assert result.error_code == "invalid_source"
            assert result.records_processed == 0 and result.failures == 1
            assert not provider.requests
            assert not result.analysis_complete
            return
        if excluded:
            assert result.state == "completed" and result.source_read_complete
            assert result.records_processed == 0 and result.records_excluded == 1
            assert not provider.requests
            return
        if budget_pause:
            assert result.state == "budget_paused" and result.error_code == "email_budget_exceeded"
            assert result.records_processed == 0 and not provider.requests
            assert result.spent_usd == result.reserved_usd == 0
            app.services.email.budget_limits = EmailBudgetLimits(
                daily_cost=Decimal("1"), monthly_cost=Decimal("1")
            )
            resume = PeopleImportRequest.model_validate(
                {
                    **request.model_dump(),
                    "phase": "resume",
                    "operation_id": result.id,
                    "expected_revision": result.revision,
                }
            )
            result = await people.create_import(
                owner, resume, key="email-resume", ceiling=Sensitivity.SENSITIVE
            )
        assert result.run_id is not None
        assert result.state == "completed", (await app.runs.get(result.run_id)).failure
        assert result.source_read_complete and result.analysis_complete
        assert result.records_processed == 1 and len(provider.requests) == passages
        assert result.spent_usd == Decimal("0.01") * passages
        assert sent_at.isoformat() in provider.requests[0].model_dump_json()
        async with app.uow_factory() as uow:
            memories = await uow.memories.list_memories(owner)
            costs = await uow.email.list(owner, "people_import_budget")
        assert len(memories) == 1 and memories[0].valid_from == sent_at
        assert len(costs) == passages and all(
            Decimal(str(cost.payload["settled_cost"])) == Decimal("0.01") for cost in costs
        )
        if passages == 4:
            assert len({attempt.run_id for attempt in provider.attempts}) == 2
