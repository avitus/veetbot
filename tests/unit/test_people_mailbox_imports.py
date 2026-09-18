"""Historical mailbox reads are explicit, governed, bounded and replayable."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.application.email import save_value
from agent_core.bootstrap import build
from agent_core.domain.email import EmailAccount
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleImportJob, PeopleQuery
from agent_core.domain.people_imports import PeopleImportRequest
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _mailbox_factory


@pytest.mark.parametrize(
    "case",
    [
        "single",
        "pages",
        "chunk",
        "outside",
        "capped",
        "wrong_account",
        "changed_body",
        "recipient",
        "cancelled",
        "retry",
        "non_object_thread",
        "missing_thread_id",
        "non_object_message",
        "missing_message_id",
        "missing_sender",
        "missing_date",
    ],
)
async def test_mailbox_import_fetches_old_source_before_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    import json

    from agent_core.adapters.determinism import SystemClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from tests.gates.test_email_runtime_m26 import _assessment_turn

    now = SystemClock().now()
    sent_at = (now - timedelta(days=200)).replace(microsecond=0)
    message: dict[str, Any] = {
        "id": "m1",
        "from": "Alex <alex@example.test>",
        "to": ["owner@example.test"],
        "cc": [],
        "body": "Alex prefers tea.",
        "headers_complete": True,
        "body_complete": True,
        "body_available": True,
        "history_id": "h1",
        "internal_date": str(int(sent_at.timestamp() * 1000)),
    }
    if case == "recipient":
        message.update(
            {"from": "owner@example.test", "to": ["alex@example.test"], "label_ids": ["SENT"]}
        )
    count = 9 if case == "pages" else 1
    values: list[tuple[str, dict[str, Any]]] = [
        (
            "get_profile",
            {
                "email_address": "other@example.test"
                if case == "wrong_account"
                else "owner@example.test"
            },
        ),
        ("search_threads", {"threads": [{"thread_id": "t1"}], "next_page_token": None}),
    ]
    malformed = case in {
        "non_object_thread",
        "missing_thread_id",
        "non_object_message",
        "missing_message_id",
        "missing_sender",
        "missing_date",
    }
    if case in {"non_object_thread", "missing_thread_id"}:
        values[1][1]["threads"] = ["invalid" if case == "non_object_thread" else {}]
    for index in range(count):
        if index == 6:
            values.append(("get_profile", {"email_address": "owner@example.test"}))
        chunk = case in {"chunk", "changed_body"}
        item = {
            **message,
            "id": f"m{index + 1}",
            "internal_date": str(
                int(
                    (
                        sent_at
                        - timedelta(hours=index)
                        + timedelta(days=2 if case == "outside" else 0)
                    ).timestamp()
                    * 1000
                )
            ),
            "body_complete": not chunk,
            "next_body_offset": len(message["body"].encode()) if chunk else None,
        }
        if case in {"missing_message_id", "missing_sender", "missing_date"}:
            item.pop(
                {
                    "missing_message_id": "id",
                    "missing_sender": "from",
                    "missing_date": "internal_date",
                }[case]
            )
        values.append(
            (
                "get_thread_page",
                {
                    "thread_id": "t1",
                    "messages": ["invalid" if case == "non_object_message" else item],
                    "next_page_token": f"page{index + 1}" if index + 1 < count else None,
                },
            )
        )
        if chunk:
            values.append(
                (
                    "get_message_body",
                    {
                        "message_id": "m1",
                        "history_id": "h1",
                        "body": "Alex enjoys hiking.",
                        "offset": len(message["body"].encode()),
                        "next_offset": None,
                        "complete": True,
                        "source_changed": case == "changed_body",
                        "body_available": True,
                    },
                )
            )
    if case == "retry":
        values.insert(2, ("get_profile", {"email_address": "owner@example.test"}))
    if case in {"cancelled", "retry"}:
        from agent_core.domain.errors import ConflictError
        from agent_core.runtime.people_mailbox_imports import _DiscoveryIO

        original_call = _DiscoveryIO.call
        interrupted = False

        async def interrupt(
            io: _DiscoveryIO,
            account_id: str,
            remote: str,
            arguments: dict[str, Any],
            *,
            operation: str | None = None,
        ) -> dict[str, Any]:
            nonlocal interrupted
            if remote == "get_thread_page" and not interrupted and case == "retry":
                interrupted = True
                raise ConflictError("synthetic transient read failure")
            value = await original_call(io, account_id, remote, arguments, operation=operation)
            if remote == "get_thread_page" and case == "cancelled":
                async with io.context.uow_factory() as uow, uow.people.lock(io.context.principal):
                    job = await io.worker.guard(uow)
                    await io.worker.save(uow, job, state="cancelled")
            return value

        monkeypatch.setattr(_DiscoveryIO, "call", interrupt)
    mailbox = await _mailbox_factory(values)
    assessment = json.loads(_assessment_turn(evidence="Alex prefers tea.").text)
    assessment["people_facts"] = []
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(text=json.dumps(assessment))
                for _ in range(count + int(case == "chunk"))
            ]
        ),
        SystemClock(),
    )
    async with build(
        settings=replace(_email_settings(), people_enabled=True),
        storage="memory",
        mcp_client_factory=mailbox,
        model_provider_overrides={"fake": provider},
    ) as app:
        owner = app.principal
        service = app.services.people
        assert service is not None
        audit = await app.services.sessions.create(owner, "general", {})
        account = next(iter(app.services.email.account_servers))
        async with app.uow_factory() as uow:
            await save_value(
                uow.email,
                owner,
                "account",
                account,
                EmailAccount(
                    id=account, label="Test", status="ready", email_address="owner@example.test"
                ),
                app.clock.now(),
            )
        from uuid import UUID

        if case == "recipient":
            from agent_core.domain.people import Person, PersonIdentifier

            async with app.uow_factory() as uow:
                common: dict[str, Any] = {
                    "tenant_id": owner.tenant_id,
                    "principal_id": owner.principal_id,
                    "created_at": app.clock.now(),
                    "updated_at": app.clock.now(),
                }
                await uow.people.put(
                    Person(id=UUID(int=1), display_name="Alex", state="active", **common),
                    expected_revision=0,
                )
                await uow.people.put(
                    PersonIdentifier(
                        id=UUID(int=2),
                        person_id=UUID(int=1),
                        identifier_kind="email",
                        namespace="email",
                        value="alex@example.test",
                        verification="owner_confirmed",
                        valid_from=sent_at - timedelta(days=1),
                        **common,
                    ),
                    expected_revision=0,
                )
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": audit.id,
                "scope": {
                    "account_ids": [account],
                    "email_source": "mailbox",
                    "person_ids": [UUID(int=1)] if case == "recipient" else [],
                    "since": sent_at - timedelta(days=1),
                    "until": sent_at + timedelta(days=1),
                    "max_records": 1 if case == "capped" else 10,
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await service.create_import(
            owner, request, key="preview", ceiling=Sensitivity.SENSITIVE
        )
        result = await service.create_import(
            owner,
            request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        if case == "retry":
            assert result.state == "failed" and result.records_processed == 0
            result = await service.create_import(
                owner,
                request.model_copy(
                    update={
                        "phase": "resume",
                        "operation_id": result.id,
                        "expected_revision": result.revision,
                    }
                ),
                key="resume",
                ceiling=Sensitivity.SENSITIVE,
            )
        if case == "cancelled":
            assert (
                result.state == "cancelled"
                and result.mailbox_records_read == 0
                and result.records_processed == 0
            )
            assert not provider.requests
            async with app.uow_factory() as uow:
                sources = await uow.people.query(
                    PeopleQuery(
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        kinds=["source"],
                        sensitivity_ceiling=Sensitivity.RESTRICTED,
                    )
                )
                assert sources == []
            return
        failed = case in {"wrong_account", "changed_body"} or malformed
        assert result.state == ("failed" if failed else "completed"), result
        expected_count = 0 if case == "wrong_account" or malformed else count
        assert result.mailbox_records_read == expected_count
        assert result.mailbox_read_complete == (not failed and case != "capped")
        expected_processed = 0 if failed or case == "outside" else count
        assert result.records_processed == expected_processed
        assert result.analysis_complete == (not failed and case != "capped")
        assert len(provider.requests) == expected_processed + int(case == "chunk")
        # The provider only receives historical passages in ascending date order,
        # even though Gmail returned newest first across two discovery slices.
        dates = []
        for prompt in provider.requests:
            text = "".join(
                part.text
                for item in prompt.conversation
                for part in getattr(item, "content", [])
                if hasattr(part, "text")
            )
            import re

            dates.extend(re.findall(r'"sent_at": "([^"\\]+)', text))
        assert len(dates) == len(provider.requests)
        assert dates == sorted(dates)
        async with app.uow_factory() as uow:
            job = await uow.people.get(owner, result.id, ceiling=Sensitivity.RESTRICTED)
            assert isinstance(job, PeopleImportJob) and job.run_id is not None
            run = await uow.runs.get(job.run_id, owner)
            if malformed:
                assert run.failure is not None
                assert run.failure.error_class == "ToolTrustRejectedError"
            assert run.tool_call_count <= 8
            sources = await uow.people.query(
                PeopleQuery(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    kinds=["source"],
                    sensitivity_ceiling=Sensitivity.RESTRICTED,
                )
            )
            assert len(sources) == expected_processed + int(case == "chunk")
            assert "Alex prefers tea" not in job.model_dump_json()
            original = await uow.email.get(owner, "account", account)
            assert original and not original.payload["history_complete"]


@pytest.mark.parametrize(
    "payload", [[], {"messages": [None]}, {"messages": "bad"}, {"messages": [{"id": "m1"}]}]
)
async def test_resumed_mailbox_header_rejects_invalid_retained_shapes(payload: Any) -> None:
    import json
    from types import SimpleNamespace
    from typing import cast

    from agent_core.domain.errors import ToolTrustRejectedError
    from agent_core.domain.events import NewEvent
    from agent_core.domain.messages import TextPart, ToolResultItem
    from agent_core.domain.people_imports import PeopleMailboxMessage
    from agent_core.domain.policies import TrustLevel
    from agent_core.runtime.people_mailbox_imports import PeopleMailboxImporter
    from tests.contract.support import SESSION_ID, memory_uow_factory, principal

    _, factory = await memory_uow_factory()
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": "mcp.gmail_read.get_thread_page",
                    "result_item": ToolResultItem(
                        call_id="header",
                        trust=TrustLevel.EXTERNAL_UNTRUSTED,
                        content=[TextPart(text=json.dumps(payload))],
                    ).model_dump(mode="json"),
                },
            )
        )
    importer = object.__new__(PeopleMailboxImporter)
    importer.context = cast(Any, SimpleNamespace(uow_factory=factory, principal=principal()))
    importer.job = cast(Any, SimpleNamespace(account_servers={"work": {"read": "gmail_read"}}))
    pending = PeopleMailboxMessage(
        message_id="m1", header_session_id=SESSION_ID, header_sequence=event.sequence, offset=1
    )
    with pytest.raises(ToolTrustRejectedError):
        await importer.header("work", pending)


@pytest.mark.parametrize("rejected_attempts", [1, 2])
@pytest.mark.parametrize("layout", ["messages", "passages"])
@pytest.mark.parametrize("rejection", ["schema_invalid", "invalid_json", "truncated", "tool_call"])
async def test_rejected_passage_assessment_pauses_the_import_for_an_exact_retry(
    rejection: str, layout: str, rejected_attempts: int
) -> None:
    """One unusable assessment pauses its import at that passage instead of failing the run."""
    import json
    import re
    from decimal import Decimal

    from agent_core.adapters.determinism import SystemClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.domain.messages import (
        FakeModelScript,
        ModelRequest,
        ModelUsage,
        ScriptedToolCall,
        ScriptedTurn,
        StopReason,
    )
    from agent_core.domain.runs import RunStatus
    from tests.gates.test_email_runtime_m26 import _assessment_turn

    now = SystemClock().now()
    sent_at = (now - timedelta(days=200)).replace(microsecond=0)
    canary = "rejected-assessment-canary"
    passages = layout == "passages"
    values: list[tuple[str, dict[str, Any]]] = [
        ("get_profile", {"email_address": "owner@example.test"}),
        ("search_threads", {"threads": [{"thread_id": "t1"}], "next_page_token": None}),
    ]
    for index in range(3):
        # m1 is newest; analysis runs oldest first, so m2 is always the second record.
        chunked = passages and index == 1
        body = "Alex prefers tea."
        values.append(
            (
                "get_thread_page",
                {
                    "thread_id": "t1",
                    "messages": [
                        {
                            "id": f"m{index + 1}",
                            "from": "Alex <alex@example.test>",
                            "to": ["owner@example.test"],
                            "cc": [],
                            "body": body,
                            "headers_complete": True,
                            "body_complete": not chunked,
                            "body_available": True,
                            "history_id": "h1",
                            "internal_date": str(
                                int((sent_at - timedelta(hours=index)).timestamp() * 1000)
                            ),
                            "next_body_offset": len(body.encode()) if chunked else None,
                        }
                    ],
                    "next_page_token": f"page{index + 1}" if index < 2 else None,
                },
            )
        )
        if chunked:
            values.append(
                (
                    "get_message_body",
                    {
                        "message_id": "m2",
                        "history_id": "h1",
                        "body": "Alex enjoys hiking.",
                        "offset": len(body.encode()),
                        "next_offset": None,
                        "complete": True,
                        "source_changed": False,
                        "body_available": True,
                    },
                )
            )
    assessment = json.loads(_assessment_turn(evidence="Alex prefers tea.").text)
    assessment["people_facts"] = []
    cost = Decimal("0.01")
    usage = ModelUsage(input_tokens=10, output_tokens=5, cost=cost)
    valid = ScriptedTurn(text=json.dumps(assessment), usage=usage)
    rejected = {
        "schema_invalid": ScriptedTurn(text=json.dumps({"summary": canary}), usage=usage),
        "invalid_json": ScriptedTurn(text=f'{{"summary": "{canary}", ', usage=usage),
        "truncated": ScriptedTurn(
            text=json.dumps({**assessment, "summary": canary})[:-20],
            stop_reason=StopReason.MAX_TOKENS,
            usage=usage,
        ),
        "tool_call": ScriptedTurn(
            text=canary,
            tool_calls=[ScriptedToolCall(name="memory.search", arguments={"query": canary})],
            usage=usage,
        ),
    }[rejection]
    # m3, then m2 (and its first passage when chunked) before the rejected passage.
    before = 2 if passages else 1
    turns = [valid] * before + [rejected] * rejected_attempts + [valid, valid]
    provider = FakeModelProvider(FakeModelScript(turns=turns), SystemClock())

    def passage(request: ModelRequest) -> tuple[list[str], list[str]]:
        text = "".join(
            part.text
            for item in request.conversation
            for part in getattr(item, "content", [])
            if hasattr(part, "text")
        )
        return re.findall(r'"id": "(m\d)"', text), re.findall(r'"body_offset": (\d+)', text)

    async with build(
        settings=replace(_email_settings(), people_enabled=True),
        storage="memory",
        mcp_client_factory=await _mailbox_factory(values),
        model_provider_overrides={"fake": provider},
    ) as app:
        owner = app.principal
        service = app.services.people
        assert service is not None
        audit = await app.services.sessions.create(owner, "general", {})
        account = next(iter(app.services.email.account_servers))
        async with app.uow_factory() as uow:
            await save_value(
                uow.email,
                owner,
                "account",
                account,
                EmailAccount(
                    id=account, label="Test", status="ready", email_address="owner@example.test"
                ),
                app.clock.now(),
            )
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": audit.id,
                "scope": {
                    "account_ids": [account],
                    "email_source": "mailbox",
                    "since": sent_at - timedelta(days=1),
                    "until": sent_at + timedelta(days=1),
                    "max_records": 10,
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await service.create_import(
            owner, request, key="preview", ceiling=Sensitivity.SENSITIVE
        )
        result = await service.create_import(
            owner,
            request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        for attempt in range(rejected_attempts):
            # The rejected passage pauses the job for an explicit retry. It is one
            # failure however many attempts reject it, and its record is unread.
            calls = before + attempt + 1
            assert result.state == "failed", result
            assert result.error_code == "analysis_incomplete"
            assert result.failures == 1
            assert result.records_read == 1 and result.records_processed == 1
            assert len(provider.requests) == calls
            if attempt:
                assert passage(provider.requests[-1]) == passage(provider.requests[-2])
            # The completed attempt stays charged to the job, the run and the
            # shared email allowance. Nothing is reserved, so resume is allowed.
            assert result.spent_usd == cost * calls and result.reserved_usd == 0
            async with app.uow_factory() as uow:
                job = await uow.people.get(owner, result.id, ceiling=Sensitivity.RESTRICTED)
                assert isinstance(job, PeopleImportJob) and job.run_id is not None
                run = await uow.runs.get(job.run_id, owner)
                assert run.status is RunStatus.COMPLETED and run.failure is None
                assert run.usage.cost == cost * (1 if attempt else calls)
                budgets = [
                    record
                    for record in await uow.email.list(owner, "people_import_budget")
                    if record.payload.get("settled_cost") is not None
                ]
                assert len(budgets) == calls
                sources = await uow.people.query(
                    PeopleQuery(
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        kinds=["source"],
                        sensitivity_ceiling=Sensitivity.RESTRICTED,
                    )
                )
                assert len(sources) == before
                # No rejected model text survives in durable progress.
                assert canary not in job.model_dump_json() + run.model_dump_json()
            result = await service.create_import(
                owner,
                request.model_copy(
                    update={
                        "phase": "resume",
                        "operation_id": result.id,
                        "expected_revision": result.revision,
                    }
                ),
                key=f"resume-{attempt}",
                ceiling=Sensitivity.SENSITIVE,
            )
        # The retry reassesses exactly the rejected passage, then the import finishes.
        calls = before + rejected_attempts + 2
        assert len(provider.requests) == calls
        assert all(len(passage(request)[0]) == 1 for request in provider.requests)
        assert passage(provider.requests[before]) == passage(provider.requests[-2])
        assert result.state == "completed" and result.error_code is None, result
        assert result.analysis_complete and result.failures == 0
        assert result.records_read == 3 and result.records_processed == 3
        assert result.spent_usd == cost * calls
