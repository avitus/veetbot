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
    malformed = case in {"non_object_thread", "missing_thread_id"}
    if malformed:
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
        values.append(
            (
                "get_thread_page",
                {
                    "thread_id": "t1",
                    "messages": [item],
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
