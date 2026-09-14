"""Owner-bounded catch-up and reuse of unchanged importance evidence."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.application.email import save_value
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EmailAccount, EmailBudgetLimits, EmailSyncState, EmailTask
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_experience_m26 import email_client
from tests.gates.test_email_learning_m26 import prepare
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _assessment_turn, _current_mail_factory, _page
from tests.gates.test_email_runtime_scheduling_m26 import _unchanged_mailbox

NOW = datetime(2026, 9, 14, 5, tzinfo=UTC)


@pytest.mark.parametrize("legacy_window", [0, 1, 2])
async def test_history_never_continues_beyond_ninety_days(legacy_window: int) -> None:
    base = await _unchanged_mailbox()
    queries: list[dict[str, Any]] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "search_threads":
                queries.append(arguments)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(NOW),
        mcp_client_factory=factory,
    ) as app:
        async with app.uow_factory() as uow:
            await save_value(
                uow.email,
                app.principal,
                "account",
                "default",
                EmailAccount(
                    id="default",
                    label="Mail",
                    history_window=legacy_window,
                    history_processed=501,
                    history_cursor="old-page",
                    inbox_cursor="old-inbox-page",
                ),
                NOW,
            )
            await save_value(
                uow.email,
                app.principal,
                "sync",
                "default",
                EmailSyncState(
                    anchor="2026-01-01", history_pending=["old-thread"], history_page_open=True
                ),
                NOW,
            )
        for _ in range(3):
            op = await app.services.email.submit_task(app.principal, kind="refresh")
            assert (await app.runs.get(op.run_id)).status is RunStatus.COMPLETED
        assert queries
        cutoff = int((NOW - timedelta(days=90)).timestamp())
        assert all(f"after:{cutoff}" in query["query"] for query in queries)
        assert all(query.get("page_token") is None for query in queries)
        assert len([query for query in queries if "in:inbox" not in query["query"]]) == 1
        async with app.uow_factory() as uow:
            account = await uow.email.get(app.principal, "account", "default")
        assert account is not None and account.payload["history_complete"] is True
        assert account.payload["history_processed"] == 0


async def test_cached_old_mail_is_not_automatically_assessed_or_removed() -> None:
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(NOW),
        mcp_client_factory=await _unchanged_mailbox(),
        script=FakeModelScript(turns=[_assessment_turn()]),
    ) as app:
        page = _page()
        page["messages"][0]["internal_date"] = int((NOW - timedelta(days=91)).timestamp() * 1000)
        thread = await app.services.email.import_thread(
            app.principal, "default", page, await app.sessions.create()
        )
        op = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(op.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 0
        async with app.uow_factory() as uow:
            assert await uow.email.get(app.principal, "thread", str(thread.id)) is not None


@pytest.mark.parametrize("relevant", [False, True])
async def test_profile_churn_reassesses_only_changed_correspondent_evidence(relevant: bool) -> None:
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(NOW),
        mcp_client_factory=await _unchanged_mailbox(),
        script=FakeModelScript(turns=[_assessment_turn(), _assessment_turn()]),
    ) as app:
        service = app.services.email
        thread = await service.import_thread(
            app.principal, "default", _page(), await app.sessions.create()
        )
        first = await service.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(first.run_id)).model_call_count == 1
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await service._put_data(
                uow.email,
                app.principal,
                "style",
                "new-style",
                {
                    "account_id": "default",
                    "recipients": ["unrelated@example.test"],
                    "sent_at": NOW.isoformat(),
                    "excerpt": "UNRELATED WRITING EXAMPLE",
                },
            )
            await service._put_data(
                uow.email,
                app.principal,
                "relationship",
                "new-partner",
                {
                    "recipients": ["colleague@example.test" if relevant else "other@example.test"],
                    "sent_at": NOW.isoformat(),
                },
            )
            await service._bump_profile(uow.email, app.principal)
        second = await service.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(second.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == (1 if relevant else 0)
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        assert all(
            "UNRELATED WRITING EXAMPLE" not in req.model_dump_json() for req in provider.requests
        )
        async with app.uow_factory() as uow:
            current = await uow.email.get(app.principal, "thread", str(thread.id))
        assert current is not None
        assert current.payload["profile_revision"] == 2


async def test_email_ceiling_returns_accounting_and_next_check_without_admitting_work() -> None:
    async with email_client() as (app, client):
        service = await prepare(app)
        service.clock = FixedClock(NOW)
        service.budget_limits = EmailBudgetLimits(
            daily_cost=Decimal("40"), monthly_cost=Decimal("400")
        )
        task = EmailTask(
            id=uuid4(),
            run_id=uuid4(),
            session_id=uuid4(),
            kind="refresh",
            account_ids=["work"],
            created_at=NOW,
            reservation=Decimal("1"),
            settled_cost=Decimal("39.04497"),
        )
        async with app.uow_factory() as uow:
            await save_value(uow.email, app.principal, "task", str(task.run_id), task, NOW)
        response = await client.post("/v1/email/refresh", json={})
        assert response.status_code == 402
        details = response.json()["error"]["details"]
        assert details["reason"] == "email_aggregate_cost"
        assert Decimal(details["daily_spent"]) == Decimal("39.04497")
        assert Decimal(details["daily_reserved"]) == 0
        assert Decimal(details["daily_limit"]) == 40
        assert Decimal(details["next_reservation"]) == 1
        assert datetime.fromisoformat(details["retry_at"]) == datetime(2026, 9, 15, tzinfo=UTC)
        assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("unresolved", [False, True])
async def test_monthly_ceiling_and_unknown_usage_do_not_promise_midnight_reset(
    unresolved: bool,
) -> None:
    async with email_client() as (app, client):
        service = await prepare(app)
        service.clock = FixedClock(NOW)
        created = NOW - timedelta(days=10)
        task = EmailTask(
            id=uuid4(),
            run_id=uuid4(),
            session_id=uuid4(),
            kind="refresh",
            account_ids=["work"],
            created_at=created,
            reservation=Decimal("200" if unresolved else "1"),
            settled_cost=None if unresolved else Decimal("199.5"),
        )
        async with app.uow_factory() as uow:
            await save_value(uow.email, app.principal, "task", str(task.run_id), task, created)
        response = await client.post("/v1/email/refresh", json={})
        assert response.status_code == 402
        details = response.json()["error"]["details"]
        retry_at = datetime.fromisoformat(details["retry_at"])
        assert retry_at == (
            NOW + timedelta(hours=1) if unresolved else created + timedelta(days=30, microseconds=1)
        )
        assert Decimal(details["daily_spent"]) == 0
        assert Decimal(details["daily_reserved"]) == (200 if unresolved else 0)


async def test_mixed_age_thread_assesses_only_recent_passages_and_cannot_auto_draft() -> None:
    base = await _current_mail_factory()
    page = _page()
    old = dict(page["messages"][0])
    old.update(
        id="old-message",
        body="OLD PRIVATE PASSAGE",
        direction="sent",
        label_ids=["SENT"],
        **{"from": "owner@example.test", "to": "colleague@example.test"},
        internal_date=int((NOW - timedelta(days=91)).timestamp() * 1000),
    )
    page["messages"].insert(0, old)
    # Exact boundary is eligible; dates older than it cannot become prompt context.
    page["messages"][1]["internal_date"] = int((NOW - timedelta(days=90)).timestamp() * 1000)

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "get_thread_page":
                return MCPCallResult(content=(json.dumps(page),), structured=page)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(NOW),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn(needs_reply=True)]),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 1
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        assert "OLD PRIVATE PASSAGE" not in provider.requests[0].model_dump_json()
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "draft") == []
            sources = await uow.email.list(app.principal, "semantic_source")
            assert len(sources) == 1
            assert await uow.email.list(app.principal, "style") == []
