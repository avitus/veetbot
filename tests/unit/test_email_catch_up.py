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


@pytest.mark.parametrize("settled", [None, "39.5"])
async def test_people_import_costs_share_email_aggregate_budget(settled: str | None) -> None:
    from agent_core.domain.email import EmailRecord

    async with email_client() as (app, client):
        service = await prepare(app)
        service.clock = FixedClock(NOW)
        service.budget_limits = EmailBudgetLimits(daily_cost=Decimal(40), monthly_cost=Decimal(400))
        async with app.uow_factory() as uow:
            await uow.email.put(
                EmailRecord(
                    tenant_id=app.principal.tenant_id,
                    principal_id=app.principal.principal_id,
                    kind="people_import_budget",
                    key="attempt-1",
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                    payload={"reservation": "39.5", "settled_cost": settled},
                ),
                expected_revision=0,
            )
        response = await client.post("/v1/email/refresh", json={})
        assert response.status_code == 402
        details = response.json()["error"]["details"]
        assert Decimal(details["daily_spent"]) == Decimal(settled or "0")
        assert Decimal(details["daily_reserved"]) == Decimal("39.5" if settled is None else "0")


@pytest.mark.parametrize("legacy_window", [0, 1, 2])
async def test_history_never_continues_beyond_ninety_days(legacy_window: int) -> None:
    """Replace legacy discovery cursors with the owner-authorized ninety-day query."""
    base = await _unchanged_mailbox()
    queries: list[dict[str, Any]] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Wrap the scripted mailbox client for the scenario-specific observations below."""
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Intercept this scenario's mailbox operation and delegate other calls."""
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
    """Retain cached old mail without spending model calls on automatic assessment."""
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
    """Reassess relevant evidence changes while ignoring unrelated profile and style churn."""
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
    """Expose budget accounting and a retry time before any over-budget task is admitted."""
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
    """Keep rolling usage and unresolved reservations distinct from daily replenishment."""
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
    """Exclude old passages from automatic learning and refuse drafts with incomplete context."""
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
        """Wrap the scripted mailbox client for the scenario-specific observations below."""
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Intercept this scenario's mailbox operation and delegate other calls."""
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


@pytest.mark.parametrize("discovery", ["inbox", "history", "complete"])
async def test_idle_discovery_advances_rolling_cutoff_without_restarting_completed_history(
    discovery: str,
) -> None:
    """Fresh searches use today's bound while covered history and cached evidence survive."""
    base = await _unchanged_mailbox()
    queries: list[dict[str, Any]] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Record query bounds while returning an unchanged mailbox."""
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Capture discovery requests without replacing the governed MCP path."""
            if name == "search_threads":
                queries.append(arguments)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    clock = FixedClock(NOW)
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=clock,
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
                    history_id="101",
                    inbox_complete=discovery != "inbox",
                    history_complete=discovery != "history",
                    history_window=0 if discovery == "history" else 1,
                    history_processed=501,
                ),
                NOW,
            )
            await save_value(
                uow.email,
                app.principal,
                "sync",
                "default",
                EmailSyncState(
                    anchor=NOW.date().isoformat(),
                    history_policy="email-history@2:90d",
                    history_since=int((NOW - timedelta(days=90)).timestamp()),
                ),
                NOW,
            )
            await app.services.email._put_data(
                uow.email,
                app.principal,
                "relationship",
                "cached-evidence",
                {"recipients": ["colleague@example.test"], "sent_at": NOW.isoformat()},
            )
        clock.advance(timedelta(days=7))
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        cutoff = int((clock.now() - timedelta(days=90)).timestamp())
        assert len(queries) == (0 if discovery == "complete" else 1)
        assert all(f"after:{cutoff}" in query["query"] for query in queries)
        async with app.uow_factory() as uow:
            sync = await uow.email.get(app.principal, "sync", "default")
            account = await uow.email.get(app.principal, "account", "default")
            evidence = await uow.email.get(app.principal, "relationship", "cached-evidence")
        assert sync is not None and sync.payload["history_since"] == cutoff
        assert account is not None and account.payload["history_processed"] == 501
        assert evidence is not None


@pytest.mark.parametrize(
    "pending", ["inbox_cursor", "history_cursor", "inbox_page_open", "history_page_open"]
)
async def test_active_discovery_keeps_query_bound_cutoff_until_pagination_finishes(
    pending: str,
) -> None:
    """A page token retains its original query, then the next idle slice advances the bound."""
    base = await _unchanged_mailbox()
    queries: list[dict[str, Any]] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Record discovery requests against an empty provider page."""
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Keep the provider response stable while observing continuation arguments."""
            if name == "search_threads":
                queries.append(arguments)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    old_cutoff = int((NOW - timedelta(days=97)).timestamp())
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(NOW),
        mcp_client_factory=factory,
    ) as app:
        account = EmailAccount(id="default", label="Mail", history_id="101")
        sync = EmailSyncState(history_policy="email-history@2:90d", history_since=old_cutoff)
        if pending.endswith("cursor"):
            account = account.model_copy(update={pending: "query-bound-page"})
        else:
            setattr(sync, pending, True)
        async with app.uow_factory() as uow:
            await save_value(uow.email, app.principal, "account", "default", account, NOW)
            await save_value(uow.email, app.principal, "sync", "default", sync, NOW)
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        assert all(f"after:{old_cutoff}" in query["query"] for query in queries)
        if pending.endswith("cursor"):
            assert any(query.get("page_token") == "query-bound-page" for query in queries)
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "sync", "default")
        assert row is not None and row.payload["history_since"] == old_cutoff
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "sync", "default")
        assert row is not None
        assert row.payload["history_since"] == int((NOW - timedelta(days=90)).timestamp())


async def _catching_up_mailbox(
    inbox: dict[str, datetime], changes: list[str], *, resync: bool = False
) -> tuple[Any, list[str]]:
    """Serve an unfinished inbox catch-up page alongside new Gmail changes."""
    base = await _current_mail_factory()
    reads: list[str] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Answer discovery, change, and thread reads while recording full reads in order."""
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Return older pages for every search so catch-up stays unfinished."""
            value: dict[str, Any]
            if name == "search_threads":
                ids = (
                    list(inbox) if "in:inbox" in arguments["query"] else ["history-0", "history-1"]
                )
                value = {
                    "threads": [{"thread_id": item} for item in ids],
                    "next_page_token": "older-page",
                }
            elif name == "sync_changes":
                value = {
                    "schema_version": 1,
                    "history_id": "101",
                    "changes": [
                        {
                            "kind": "message_added",
                            "thread_id": item,
                            "message_id": f"{item}-m",
                            "label_ids": ["INBOX"],
                        }
                        for item in changes
                    ],
                    "next_page_token": None,
                    "resync_required": resync,
                }
            elif name == "get_thread_page":
                thread_id = arguments["thread_id"]
                reads.append(thread_id)
                value = _page()
                value["thread_id"] = thread_id
                value["messages"][0].update(
                    thread_id=thread_id,
                    id=f"{thread_id}-m",
                    internal_date=int(inbox.get(thread_id, NOW).timestamp() * 1000),
                )
            else:
                return await original(name, arguments)
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    return factory, reads


async def test_new_mail_is_read_while_inbox_catch_up_continues() -> None:
    """New Gmail changes are read first, and catch-up and history still keep their shares."""
    inbox = {f"inbox-{index}": NOW - timedelta(days=10 + index) for index in range(8)}
    changes = [f"change-{index}" for index in range(6)]
    factory, reads = await _catching_up_mailbox(inbox, changes)
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(NOW),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(4)]),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            account = await uow.email.get(app.principal, "account", "default")
            sync = await uow.email.get(app.principal, "sync", "default")
    assert account is not None and sync is not None
    assert reads[:4] == changes[:4]
    assert len([item for item in reads if item.startswith("inbox-")]) >= 4
    assert {"history-0", "history-1"} <= set(reads)
    assert len(reads) <= 10
    assert sync.payload["change_pending"] == ["change-4", "change-5"]
    assert account.payload["inbox_complete"] is False


@pytest.mark.parametrize("restart", ["none", "resync", "policy"])
async def test_catch_up_progress_reports_cutoff_and_oldest_inbox_mail_reached(
    restart: str,
) -> None:
    """Expose the ninety-day floor and how far back the current inbox pass has reached."""
    inbox = {
        "inbox-0": NOW - timedelta(days=3),
        "inbox-1": NOW - timedelta(days=40),
        "inbox-2": NOW - timedelta(days=20),
    }
    factory, _ = await _catching_up_mailbox(inbox, [], resync=restart == "resync")
    earlier = NOW - timedelta(days=75)
    cutoff = NOW - timedelta(days=90)
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(NOW),
        mcp_client_factory=factory,
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(4)]),
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
                    history_id="101",
                    inbox_cursor="older-page",
                    inbox_reached_at=earlier,
                ),
                NOW,
            )
            await save_value(
                uow.email,
                app.principal,
                "sync",
                "default",
                EmailSyncState(anchor="2026-01-01")
                if restart == "policy"
                else EmailSyncState(
                    history_policy="email-history@2:90d",
                    history_since=int(cutoff.timestamp()),
                ),
                NOW,
            )
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        body = await app.services.email.accounts(app.principal)
    items = body["items"]
    assert isinstance(items, list)
    item = items[0]
    assert item["inbox_complete"] is False
    assert datetime.fromisoformat(item["catch_up_since"]) == cutoff
    reached = datetime.fromisoformat(item["inbox_reached_at"])
    assert reached == (earlier if restart == "none" else NOW - timedelta(days=40))
