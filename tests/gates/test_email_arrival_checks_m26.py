"""ADR-0286: the server checks for new mail while Email is closed."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.application.email import read_value, save_value
from agent_core.application.notification_relevance import notification_obsolete
from agent_core.bootstrap import Composition, build
from agent_core.config import ConfigurationError, load_config_document, load_settings
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import (
    EmailAccount,
    EmailBudgetLimits,
    EmailSyncState,
    EmailTask,
)
from agent_core.domain.errors import NotFoundError
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript, ModelPermanentError, ScriptedTurn
from agent_core.domain.notifications import NotificationKind
from agent_core.domain.runs import RunStatus
from agent_core.runtime.email_state import records
from agent_core.runtime.worker import MaintenanceWorker
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _assessment_turn, _current_mail_factory, _page

NOW = datetime(2026, 9, 11, 13, 0, tzinfo=UTC)
FORMATION = "memory.formation.requested"


async def _mailbox(changes: list[str], calls: list[tuple[str, str]]) -> Any:
    """Report each change once, and record every provider call a check makes."""
    base = await _current_mail_factory()
    pending = list(changes)

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Wrap the scripted client so the test sees each call."""
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            """Record the call, answering sync_changes from the pending changes."""
            calls.append((name, str(arguments.get("thread_id", ""))))
            if name == "sync_changes":
                value: dict[str, Any] = {
                    "schema_version": 1,
                    "history_id": "101",
                    "changes": [
                        {
                            "kind": "message_added",
                            "thread_id": item,
                            "message_id": f"{item}-m",
                            "label_ids": ["INBOX"],
                        }
                        for item in pending
                    ],
                    "next_page_token": None,
                    "resync_required": False,
                }
                pending.clear()
            elif name == "get_thread_page":
                value = _thread_page(arguments["thread_id"], NOW)
            else:
                return await original(name, arguments)
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


def _thread_page(thread_id: str, when: datetime) -> dict[str, Any]:
    page = _page()
    page["thread_id"] = thread_id
    page["messages"][0].update(
        thread_id=thread_id,
        id=f"{thread_id}-m",
        internal_date=int(when.timestamp() * 1000),
        label_ids=["INBOX"],
    )
    return page


async def _caught_up(app: Composition, **account: Any) -> None:
    """An account with nothing left to catch up on, so only new Gmail changes matter."""
    fields = {"inbox_complete": True, "history_complete": True, "history_id": "100", **account}
    async with app.uow_factory() as uow:
        await save_value(
            uow.email,
            app.principal,
            "account",
            "default",
            EmailAccount(id="default", label="Mail", **fields),
            NOW,
        )
        await save_value(
            uow.email,
            app.principal,
            "sync",
            "default",
            EmailSyncState(
                history_policy="email-history@2:90d",
                history_since=int((NOW - timedelta(days=90)).timestamp()),
            ),
            NOW,
        )


def _app(clock: FixedClock, factory: Any, *turns: ScriptedTurn) -> Any:
    return build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=clock,
        mcp_client_factory=factory,
        script=FakeModelScript(turns=list(turns)),
    )


def _limit(app: Composition) -> None:
    app.services.email.budget_limits = EmailBudgetLimits(
        daily_cost=Decimal("40"), monthly_cost=Decimal("600")
    )


async def _seed_spend(app: Composition, spent: str, created: datetime) -> None:
    """Spending that is not a refresh, so it can only affect the budget."""
    task = EmailTask(
        id=uuid4(),
        run_id=uuid4(),
        session_id=uuid4(),
        kind="draft",
        account_ids=["default"],
        created_at=created,
        reservation=Decimal("1"),
        settled_cost=Decimal(spent),
    )
    async with app.uow_factory() as uow:
        await save_value(uow.email, app.principal, "task", str(task.run_id), task, created)


async def _run_of(app: Composition, run_id: Any) -> Any:
    return await app.runs.get(run_id)


async def _event_types(app: Composition, run: Any) -> list[str]:
    """The event types recorded for a run."""
    async with app.uow_factory() as uow:
        events = await uow.events.list_after(run.session_id, 0, app.principal, run_id=run.id)
    return [event.event_type for event in events]


async def _assert_a_check_is_admitted_only_when_due() -> None:
    """A check waits its interval, and any recent refresh defers it."""
    clock = FixedClock(NOW)
    async with _app(clock, await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        first = await service.admit_arrival_check(app.principal)
        assert first is not None
        task = await service.get_task(app.principal, first.run_id)
        assert task is not None and task.scope == "arrivals"
        assert (await _run_of(app, first.run_id)).priority == 10

        clock.advance(timedelta(seconds=299))
        assert await service.admit_arrival_check(app.principal) is None
        clock.advance(timedelta(seconds=1))
        assert await service.admit_arrival_check(app.principal) is not None

        # A foreground refresh of any scope makes the next check unnecessary.
        clock.advance(timedelta(seconds=299))
        foreground = await service.submit_task(app.principal, kind="refresh")
        clock.advance(timedelta(seconds=299))
        assert await service.admit_arrival_check(app.principal) is None
        recorded = await service.get_task(app.principal, foreground.run_id)
        assert recorded is not None and recorded.scope == "full"


async def _assert_a_check_handles_only_arrivals() -> None:
    """A check handles new Inbox arrivals and leaves other mail alone."""
    clock = FixedClock(NOW)
    calls: list[tuple[str, str]] = []
    turns = [_assessment_turn() for _ in range(5)]
    async with _app(clock, await _mailbox([], calls), *turns) as app:
        await _caught_up(app)
        service = app.services.email
        session = await app.sessions.create()
        known = await service.import_thread(
            app.principal, "default", _thread_page("known", NOW), session
        )
        foreground = await service.submit_task(app.principal, kind="refresh")
        assert (await _run_of(app, foreground.run_id)).model_call_count == 1
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await service._bump_profile(uow.email, app.principal)
        old = await service.import_thread(
            app.principal, "default", _thread_page("old", NOW - timedelta(days=30)), session
        )
        arrival = await service.import_thread(
            app.principal, "default", _thread_page("arrival", NOW), session
        )
        clock.advance(timedelta(minutes=6))

        check = await service.admit_arrival_check(app.principal)

        assert check is not None
        run = await _run_of(app, check.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 1
        async with app.uow_factory() as uow:
            assert await uow.email.get(app.principal, "assessment", str(arrival.id)) is not None
            assert await uow.email.get(app.principal, "assessment", str(old.id)) is None
            stale = await uow.email.get(app.principal, "assessment", str(known.id))
        assert stale is not None and stale.payload["profile_revision"] == 1


async def _assert_a_check_assesses_at_most_four_and_skips_foreground_work() -> None:
    """One check assesses at most four threads and leaves foreground work undone."""
    clock = FixedClock(NOW - timedelta(days=31))
    calls: list[tuple[str, str]] = []
    changes = [f"change-{index}" for index in range(6)]
    turns = [_assessment_turn() for _ in range(6)]
    async with _app(clock, await _mailbox(changes, calls), *turns) as app:
        await _caught_up(app, inbox_complete=False, inbox_cursor="older-page")
        service = app.services.email
        session = await app.sessions.create()
        # A body past its thirty-day window: only a foreground slice re-reads it.
        await service.import_thread(
            app.principal, "default", _thread_page("expired", NOW - timedelta(days=40)), session
        )
        clock.advance(timedelta(days=31))

        check = await service.admit_arrival_check(app.principal)

        assert check is not None
        run = await _run_of(app, check.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 4
    assert sorted(item for name, item in calls if name == "get_thread_page") == sorted(changes)
    assert not [name for name, _ in calls if name in {"search_threads", "get_unsubscribe"}]


async def _assert_a_check_drafts_only_what_it_assessed() -> None:
    """A check prepares drafts only for threads it assessed itself."""
    clock = FixedClock(NOW)
    turns = [
        _assessment_turn(needs_reply=True),
        ScriptedTurn(text='{"body": ""}'),
        _assessment_turn(needs_reply=True),
        ScriptedTurn(text=json.dumps({"body": "Thanks. I will review the materials."})),
    ]
    async with _app(clock, await _mailbox([], []), *turns) as app:
        await _caught_up(app)
        service = app.services.email
        session = await app.sessions.create()
        undrafted = await service.import_thread(
            app.principal, "default", _thread_page("undrafted", NOW), session
        )
        # A rejected proposal leaves this conversation eligible and undrafted.
        await service.submit_task(app.principal, kind="refresh")
        arrival = await service.import_thread(
            app.principal, "default", _thread_page("arrival", NOW), session
        )
        clock.advance(timedelta(minutes=6))

        check = await service.admit_arrival_check(app.principal)

        assert check is not None
        assert (await _run_of(app, check.run_id)).model_call_count == 2
        async with app.uow_factory() as uow:
            drafts = await uow.email.list(app.principal, "draft")
        assert [row.payload["thread_id"] for row in drafts] == [str(arrival.id)]
        assert str(undrafted.id) not in {row.payload["thread_id"] for row in drafts}


async def _assert_a_check_stops_at_its_own_limits(today: str, earlier: str, admitted: bool) -> None:
    clock = FixedClock(NOW)
    async with _app(clock, await _mailbox([], [])) as app:
        await _caught_up(app)
        _limit(app)
        service = app.services.email
        await _seed_spend(app, today, NOW - timedelta(minutes=30))
        await _seed_spend(app, earlier, NOW - timedelta(days=10))

        check = await service.admit_arrival_check(app.principal)

        assert (check is not None) is admitted
        # A refused check writes no pause, and the owner can still refresh.
        clock.advance(timedelta(minutes=1))
        foreground = await service.submit_task(app.principal, kind="refresh")
        assert (await _run_of(app, foreground.run_id)).status is RunStatus.COMPLETED


async def _assert_budget_limits() -> None:
    """A check stops at its own share of the daily and rolling allowances."""
    for today, earlier, admitted in [
        ("19", "0", True),
        ("19.5", "0", False),
        ("0", "539", True),
        ("0", "539.5", False),
    ]:
        await _assert_a_check_stops_at_its_own_limits(today, earlier, admitted)


async def _seed_refresh(
    app: Composition,
    base: Any,
    created: datetime,
    status: RunStatus,
    charge: str | None = None,
    *,
    scope: Literal["full", "new_mail", "arrivals"] = "arrivals",
) -> None:
    """Store a refresh task and its run, as an earlier check or refresh would leave them."""
    run = base.model_copy(update={"id": uuid4(), "status": status})
    task = EmailTask(
        id=uuid4(),
        run_id=run.id,
        session_id=run.session_id,
        kind="refresh",
        account_ids=["default"],
        created_at=created,
        reservation=Decimal("1"),
        settled_cost=Decimal("0") if charge is None else None,
        budget_charge=None if charge is None else Decimal(charge),
        scope=scope,
    )
    async with app.uow_factory() as uow:
        await uow.runs.create(run)
        await save_value(uow.email, app.principal, "task", str(run.id), task, created)


async def _assert_failures_widen_the_interval() -> None:
    """Consecutive failures widen the wait up to its limit, and a success resets it."""
    for failures, wait in [(1, 10), (2, 20), (3, 40), (4, 60), (6, 60)]:
        clock = FixedClock(NOW - timedelta(hours=3))
        async with _app(clock, await _mailbox([], [])) as app:
            await _caught_up(app)
            service = app.services.email
            first = await service.admit_arrival_check(app.principal)
            assert first is not None
            base = await _run_of(app, first.run_id)
            clock.advance(timedelta(hours=1))
            for index in range(failures):
                status = RunStatus.CANCELLED if index % 2 else RunStatus.FAILED
                await _seed_refresh(app, base, clock.now() + timedelta(seconds=index), status)
            clock.advance(timedelta(seconds=failures - 1))
            clock.advance(timedelta(minutes=wait, seconds=-1))
            assert await service.admit_arrival_check(app.principal) is None
            clock.advance(timedelta(seconds=1))
            assert await service.admit_arrival_check(app.principal) is not None

    # A conservative charge counts as a failure; a completed refresh resets the count.
    clock = FixedClock(NOW - timedelta(hours=3))
    async with _app(clock, await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        first = await service.admit_arrival_check(app.principal)
        assert first is not None
        base = await _run_of(app, first.run_id)
        clock.advance(timedelta(hours=1))
        await _seed_refresh(app, base, clock.now(), RunStatus.COMPLETED, charge="1")
        clock.advance(timedelta(minutes=9))
        assert await service.admit_arrival_check(app.principal) is None
        await _seed_refresh(app, base, clock.now(), RunStatus.COMPLETED)
        clock.advance(timedelta(minutes=5))
        assert await service.admit_arrival_check(app.principal) is not None


async def _assert_a_check_with_nothing_new_stays_cheap() -> None:
    """With no changed mail a check makes no model call, memory request or notification."""
    clock = FixedClock(NOW)
    calls: list[tuple[str, str]] = []

    def guarded(store: Any, principal: Any, kind: str) -> Any:
        assert kind != "thread", "an empty check must not load stored message content"
        return records(store, principal, kind)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("agent_core.runtime.email_tasks.records", guarded)
        async with _app(clock, await _mailbox([], calls)) as app:
            await _caught_up(app)
            service = app.services.email

            check = await service.admit_arrival_check(app.principal)

            assert check is not None
            run = await _run_of(app, check.run_id)
            assert run.status is RunStatus.COMPLETED
            assert run.model_call_count == 0
            task = await service.get_task(app.principal, run.id)
            assert task is not None and task.empty_check is True
            assert FORMATION not in await _event_types(app, run)
            async with app.uow_factory() as uow:
                assert await uow.notification_outbox.list(app.principal, limit=10) == []
    assert {name for name, _ in calls} == {"get_profile", "sync_changes"}


async def _assert_a_check_that_found_mail_is_not_empty() -> None:
    """A check that assessed mail is not marked empty, and still notifies nobody."""
    clock = FixedClock(NOW)
    async with _app(clock, await _mailbox(["new"], []), _assessment_turn()) as app:
        await _caught_up(app)
        service = app.services.email

        check = await service.admit_arrival_check(app.principal)

        assert check is not None
        run = await _run_of(app, check.run_id)
        assert run.model_call_count == 1
        task = await service.get_task(app.principal, run.id)
        assert task is not None and task.empty_check is False
        assert FORMATION in await _event_types(app, run)
        async with app.uow_factory() as uow:
            assert await uow.notification_outbox.list(app.principal, limit=10) == []


async def _assert_empty_checks_are_removed_after_seven_days() -> None:
    clock = FixedClock(NOW)
    async with _app(clock, await _mailbox(["new"], []), _assessment_turn()) as app:
        await _caught_up(app)
        service = app.services.email
        kept = await service.admit_arrival_check(app.principal)
        assert kept is not None
        clock.advance(timedelta(minutes=5))
        empty = await service.admit_arrival_check(app.principal)
        assert empty is not None
        session_id = (await _run_of(app, empty.run_id)).session_id

        clock.advance(timedelta(days=6))
        assert await service.prune_empty_checks(app.principal) == 0
        clock.advance(timedelta(days=1, minutes=1))
        assert await service.prune_empty_checks(app.principal) == 1

        async with app.uow_factory() as uow:
            assert (
                await read_value(uow.email, app.principal, "task", str(empty.run_id), EmailTask)
                is None
            )
            assert (
                await read_value(uow.email, app.principal, "task", str(kept.run_id), EmailTask)
                is not None
            )
            with pytest.raises(NotFoundError):
                await uow.sessions.get(session_id, app.principal)
        assert await service.prune_empty_checks(app.principal) == 0


async def _assert_an_empty_check_missed_for_months_is_still_removed() -> None:
    """A check older than any accounting window is still found once maintenance returns."""
    clock = FixedClock(NOW)
    async with _app(clock, await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        empty = await service.admit_arrival_check(app.principal)
        assert empty is not None

        clock.advance(timedelta(days=90))

        assert await service.prune_empty_checks(app.principal) == 1
        async with app.uow_factory() as uow:
            assert (
                await read_value(uow.email, app.principal, "task", str(empty.run_id), EmailTask)
                is None
            )


async def _assert_a_failed_check_reaches_nobody() -> None:
    """A failure leaves an audit row that dispatch skips as obsolete, so no alert is sent."""
    clock = FixedClock(NOW)
    rejected = ModelPermanentError(
        provider="fake",
        model="scripted",
        attempt_id=uuid4(),
        message="The model provider rejected the request.",
        http_status=400,
        provider_code="invalid_json_schema",
    )
    async with build(
        settings=replace(
            _email_settings(),
            email_mode_enabled=True,
            notification_api_enabled=True,
            notification_dispatch_enabled=True,
        ),
        clock=clock,
        mcp_client_factory=await _mailbox(["new"], []),
        script=FakeModelScript(turns=[ScriptedTurn(fail_with=rejected)]),
    ) as app:
        await _caught_up(app)

        check = await app.services.email.admit_arrival_check(app.principal)

        assert check is not None
        assert (await _run_of(app, check.run_id)).status is RunStatus.FAILED
        async with app.uow_factory() as uow:
            rows = await uow.notification_outbox.list(app.principal, limit=10)
            assert [row.kind for row in rows] == [NotificationKind.RUN_FAILED]
            assert await notification_obsolete(uow, rows[0], clock.now())


async def _assert_a_failed_foreground_refresh_is_not_a_failed_check() -> None:
    """It defers a check for the normal interval and leaves the wait unwidened."""
    clock = FixedClock(NOW - timedelta(hours=3))
    async with _app(clock, await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        first = await service.admit_arrival_check(app.principal)
        assert first is not None
        base = await _run_of(app, first.run_id)
        clock.advance(timedelta(hours=1))
        await _seed_refresh(app, base, clock.now(), RunStatus.FAILED, scope="full")

        clock.advance(timedelta(minutes=5, seconds=-1))
        assert await service.admit_arrival_check(app.principal) is None
        clock.advance(timedelta(seconds=1))
        assert await service.admit_arrival_check(app.principal) is not None


async def _assert_a_completed_foreground_refresh_resets_the_wait() -> None:
    """Any completed refresh resets the count, even one that took a conservative charge."""
    clock = FixedClock(NOW - timedelta(hours=3))
    async with _app(clock, await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        first = await service.admit_arrival_check(app.principal)
        assert first is not None
        base = await _run_of(app, first.run_id)
        clock.advance(timedelta(hours=1))
        await _seed_refresh(app, base, clock.now(), RunStatus.FAILED)
        clock.advance(timedelta(minutes=1))
        await _seed_refresh(app, base, clock.now(), RunStatus.COMPLETED, charge="1", scope="full")

        clock.advance(timedelta(minutes=5, seconds=-1))
        assert await service.admit_arrival_check(app.principal) is None
        clock.advance(timedelta(seconds=1))
        assert await service.admit_arrival_check(app.principal) is not None


CHECKS: list[Callable[[], Awaitable[None]]] = [
    _assert_a_check_is_admitted_only_when_due,
    _assert_a_check_handles_only_arrivals,
    _assert_a_check_assesses_at_most_four_and_skips_foreground_work,
    _assert_a_check_drafts_only_what_it_assessed,
    _assert_budget_limits,
    _assert_failures_widen_the_interval,
    _assert_a_failed_check_reaches_nobody,
    _assert_a_failed_foreground_refresh_is_not_a_failed_check,
    _assert_a_completed_foreground_refresh_resets_the_wait,
    _assert_a_check_with_nothing_new_stays_cheap,
    _assert_a_check_that_found_mail_is_not_empty,
    _assert_empty_checks_are_removed_after_seven_days,
    _assert_an_empty_check_missed_for_months_is_still_removed,
]


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__.removeprefix("_assert_"))
async def test_arrival_check_gate(check: Callable[[], Awaitable[None]]) -> None:
    await check()


def test_the_flag_needs_both_email_flags() -> None:
    environment = {
        "DATABASE_URL": "postgresql+asyncpg://127.0.0.1:1/unused",
        "DEPLOYMENT_MODE": "development",
        "AUTH_MODE": "dev",
        "AGENT_SANDBOX": "fake",
    }
    assert load_settings(environment).email_background_checks_enabled is False
    with pytest.raises(ConfigurationError, match="AGENT_EMAIL_BACKGROUND_CHECKS_ENABLED"):
        load_settings({**environment, "AGENT_EMAIL_BACKGROUND_CHECKS_ENABLED": "1"})


@pytest.mark.parametrize("enabled", [False, True])
async def test_only_the_flag_gives_maintenance_a_check_sweep(enabled: bool) -> None:
    """Maintenance gets a check sweep only when the flag is on."""
    settings = replace(
        _email_settings(), email_mode_enabled=True, email_background_checks_enabled=enabled
    )
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[]),
        mcp_client_factory=await _mailbox([], []),
    ) as app:
        await _caught_up(app)
        sweep = cast(MaintenanceWorker, app.maintenance_factory())._sweep_email_check
        assert (sweep is not None) is enabled
        if sweep is not None:
            # The maintenance principal itself holds the scopes a check needs.
            assert await sweep() == 1


def test_the_shipped_interval_is_five_minutes() -> None:
    limits = EmailBudgetLimits(daily_cost=Decimal("1"), monthly_cost=Decimal("1"))
    shipped = load_config_document(_email_settings(), "runtime/limits.yaml")["email"]
    assert limits.check_interval_seconds == shipped["check_interval_seconds"] == 300
