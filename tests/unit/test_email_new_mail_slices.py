"""ADR-0298: above the learning share a refresh spends only on mail that arrived."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import Composition
from agent_core.domain.email import EmailBudgetLimits
from agent_core.domain.messages import ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_arrival_checks_m26 import (
    FORMATION,
    NOW,
    _app,
    _assessment_of,
    _caught_up,
    _event_types,
    _limit,
    _long_page,
    _mailbox,
    _run_of,
    _seed_spend,
    _thread_page,
)
from tests.gates.test_email_runtime_m26 import _assessment_turn


async def _above_the_share(app: Composition) -> None:
    """USD 460 of USD 600: past the USD 450 share and short of the ceiling."""
    _limit(app)
    await _seed_spend(app, "460", NOW - timedelta(days=10))


async def _new_mail_slice(app: Composition) -> Any:
    """Admit one refresh, confirm its scope, and return its completed run."""
    operation = await app.services.email.submit_task(app.principal, kind="refresh")
    run = await _run_of(app, operation.run_id)
    assert run.status is RunStatus.COMPLETED, run.failure
    task = await app.services.email.get_task(app.principal, run.id)
    assert task is not None and task.scope == "new_mail"
    return run


async def test_a_refresh_with_no_new_mail_spends_nothing() -> None:
    """A profile change alone costs no model call, leaves no trace to keep or learn from."""
    clock = FixedClock(NOW)
    calls: list[tuple[str, str]] = []
    turns = [_assessment_turn() for _ in range(3)]
    async with _app(clock, await _mailbox([], calls), *turns) as app:
        await _caught_up(app)
        service = app.services.email
        session = await app.sessions.create()
        known = await service.import_thread(
            app.principal, "default", _thread_page("known", NOW), session
        )
        await service.submit_task(app.principal, kind="refresh")
        before = await _assessment_of(app, known)
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await service._bump_profile(uow.email, app.principal)
        await _above_the_share(app)
        clock.advance(timedelta(minutes=1))
        calls.clear()

        run = await _new_mail_slice(app)

        assert run.model_call_count == 0
        assert await _assessment_of(app, known) == before
        task = await service.get_task(app.principal, run.id)
        assert task is not None and task.empty_check is True
        assert FORMATION not in await _event_types(app, run)
        assert {name for name, _ in calls} == {"get_profile", "sync_changes"}

        clock.advance(timedelta(days=7, minutes=1))
        assert await service.prune_empty_checks(app.principal) == 1
        assert await service.get_task(app.principal, run.id) is None


async def test_a_new_mail_slice_assesses_only_arrivals() -> None:
    """Recent unassessed mail is ranked; older mail and profile changes wait for a full slice."""
    clock = FixedClock(NOW)
    turns = [_assessment_turn() for _ in range(5)]
    async with _app(clock, await _mailbox([], []), *turns) as app:
        await _caught_up(app)
        service = app.services.email
        session = await app.sessions.create()
        known = await service.import_thread(
            app.principal, "default", _thread_page("known", NOW), session
        )
        await service.submit_task(app.principal, kind="refresh")
        before = await _assessment_of(app, known)
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await service._bump_profile(uow.email, app.principal)
        old = await service.import_thread(
            app.principal, "default", _thread_page("old", NOW - timedelta(days=8)), session
        )
        arrival = await service.import_thread(
            app.principal, "default", _thread_page("arrival", NOW), session
        )
        await _above_the_share(app)
        clock.advance(timedelta(minutes=1))

        run = await _new_mail_slice(app)

        assert run.model_call_count == 1
        task = await service.get_task(app.principal, run.id)
        assert task is not None and task.empty_check is False
        assert FORMATION in await _event_types(app, run)
        assert await _assessment_of(app, known) == before
        async with app.uow_factory() as uow:
            assert await uow.email.get(app.principal, "assessment", str(arrival.id)) is not None
            assert await uow.email.get(app.principal, "assessment", str(old.id)) is None


async def test_a_long_conversation_gets_one_pass_until_a_full_slice() -> None:
    """Later passes wait above the share and resume where they stopped once a full slice runs."""
    clock = FixedClock(NOW)
    turns = [_assessment_turn() for _ in range(4)]
    async with _app(clock, await _mailbox([], []), *turns) as app:
        await _caught_up(app)
        service = app.services.email
        session = await app.sessions.create()
        thread = await service.import_thread(
            app.principal, "default", _long_page("long", NOW), session
        )
        await _above_the_share(app)

        assert (await _new_mail_slice(app)).model_call_count == 1
        first = await _assessment_of(app, thread)
        assert first["analysis_complete"] is False
        clock.advance(timedelta(minutes=2))
        assert (await _new_mail_slice(app)).model_call_count == 0
        assert await _assessment_of(app, thread) == first

        service.budget_limits = EmailBudgetLimits(
            daily_cost=Decimal("4000"), monthly_cost=Decimal("6000")
        )
        clock.advance(timedelta(minutes=2))
        operation = await service.submit_task(app.principal, kind="refresh")
        full = await service.get_task(app.principal, operation.run_id)
        assert full is not None and full.scope == "full"
        assert (await _run_of(app, operation.run_id)).model_call_count == 1
        later = await _assessment_of(app, thread)
        assert later["analysis_cursor"] > first["analysis_cursor"]


async def test_a_new_mail_slice_drafts_only_what_it_assessed() -> None:
    """A rejected draft is not retried on every refresh above the share."""
    clock = FixedClock(NOW)
    reply = ScriptedTurn(text=json.dumps({"body": "Thanks. I will review the materials."}))
    turns = [
        _assessment_turn(needs_reply=True),
        ScriptedTurn(text='{"body": ""}'),
        _assessment_turn(needs_reply=True),
        reply,
        reply,
    ]
    async with _app(clock, await _mailbox([], []), *turns) as app:
        await _caught_up(app)
        service = app.services.email
        session = await app.sessions.create()
        await service.import_thread(
            app.principal, "default", _thread_page("undrafted", NOW), session
        )
        await service.submit_task(app.principal, kind="refresh")
        arrival = await service.import_thread(
            app.principal, "default", _thread_page("arrival", NOW), session
        )
        await _above_the_share(app)
        clock.advance(timedelta(minutes=1))

        run = await _new_mail_slice(app)

        assert run.model_call_count == 2
        async with app.uow_factory() as uow:
            drafts = await uow.email.list(app.principal, "draft")
        assert [row.payload["thread_id"] for row in drafts] == [str(arrival.id)]


async def test_a_new_mail_slice_that_verified_a_subscription_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsubscribe evidence is still read, and a slice that read it is not an empty look."""
    clock = FixedClock(NOW)
    calls: list[tuple[str, str]] = []
    async with _app(clock, await _mailbox([], calls)) as app:
        await _caught_up(app)
        subscriptions = app.services.email.subscriptions
        applied: list[str] = []

        async def unverified(*_args: Any) -> list[tuple[str, str]]:
            return [] if applied else [("subscription", "message")]

        async def apply_verification(_principal: Any, identifier: str, *_: Any, **__: Any) -> None:
            applied.append(identifier)

        monkeypatch.setattr(subscriptions, "unverified", unverified)
        monkeypatch.setattr(subscriptions, "apply_verification", apply_verification)
        await _above_the_share(app)

        run = await _new_mail_slice(app)

        assert run.model_call_count == 0
        assert ("get_unsubscribe", "") in calls
        task = await app.services.email.get_task(app.principal, run.id)
        assert task is not None and task.empty_check is False


async def test_a_full_slice_still_brings_a_conversation_up_to_date() -> None:
    """Below the share a profile change is still applied, so ratings catch up."""
    clock = FixedClock(NOW)
    turns = [_assessment_turn() for _ in range(2)]
    async with _app(clock, await _mailbox([], []), *turns) as app:
        await _caught_up(app)
        service = app.services.email
        session = await app.sessions.create()
        known = await service.import_thread(
            app.principal, "default", _thread_page("known", NOW), session
        )
        await service.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            bumped = await service._bump_profile(uow.email, app.principal)
        clock.advance(timedelta(minutes=1))

        operation = await service.submit_task(app.principal, kind="refresh")

        task = await service.get_task(app.principal, operation.run_id)
        assert task is not None and task.scope == "full"
        assert (await _assessment_of(app, known))["profile_revision"] == bumped.profile_revision
