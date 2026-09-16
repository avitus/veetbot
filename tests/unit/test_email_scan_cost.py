"""Mailbox-wide scans stay off request paths without widening body retention."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.persistence.email import InMemoryEmailStore
from agent_core.application.email import save_value
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailDraftStatus, EmailRecord, EmailThread
from agent_core.domain.messages import FakeModelScript, ModelRequest, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import MaintenanceWorker
from tests.gates.test_email_experience_m26 import email_client, seed_mail
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import (
    _assessment_turn,
    _current_mail_factory,
    _page,
    _seed_draft,
)
from tests.unit.test_maintenance_approval_reaper import _script, _settings

MAILBOX = {"thread", "draft", "draft_revision"}
PROVIDER_BODY = _page()["messages"][0]["body"]


def record_scans(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    scanned: list[str] = []
    original = InMemoryEmailStore.list

    async def recording(
        store: InMemoryEmailStore,
        principal: Principal,
        kind: str,
        *,
        after: str | None = None,
        limit: int = 1000,
    ) -> list[EmailRecord]:
        scanned.append(kind)
        return await original(store, principal, kind, after=after, limit=limit)

    monkeypatch.setattr(InMemoryEmailStore, "list", recording)
    return scanned


def record_model_requests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    rendered: list[str] = []
    original = FakeModelProvider._next_turn

    def recording(provider: FakeModelProvider, request: ModelRequest) -> ScriptedTurn:
        rendered.append(request.model_dump_json())
        return original(provider, request)

    monkeypatch.setattr(FakeModelProvider, "_next_turn", recording)
    return rendered


async def expire(app: Composition, thread_id: UUID, **changes: Any) -> None:
    """Age one cached conversation past the body window without running maintenance."""
    async with app.uow_factory() as uow, uow.email.lock(app.principal):
        row = await uow.email.get(app.principal, "thread", str(thread_id))
        assert row is not None
        thread = EmailThread.model_validate(row.payload)
        aged = thread.model_copy(
            update={"last_accessed_at": app.clock.now() - timedelta(days=31), **changes}
        )
        await save_value(uow.email, app.principal, "thread", str(thread_id), aged, app.clock.now())


@pytest.mark.parametrize("kind", ["refresh", "draft", "archive"])
async def test_admission_does_not_scan_the_mailbox(
    monkeypatch: pytest.MonkeyPatch, kind: Literal["refresh", "draft", "archive"]
) -> None:
    """Retention belongs to maintenance; admitting one operation reads only its own mail."""
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _current_mail_factory(),
    ) as app:
        thread, _ = await _seed_draft(app)
        dispatched: list[UUID] = []

        async def dispatch(run_id: UUID) -> None:
            dispatched.append(run_id)

        service = app.services.email
        service.dispatch = dispatch
        scanned = record_scans(monkeypatch)
        if kind == "archive":
            await service.archive(
                app.principal, thread.id, thread.revision, archived=True, idempotency_key="once"
            )
        else:
            await service.submit_task(
                app.principal, kind=kind, thread_id=None if kind == "refresh" else thread.id
            )
        assert len(dispatched) == 1
        assert not MAILBOX & set(scanned)


async def test_endorsement_does_not_scan_the_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    async with email_client() as (app, _):
        _, draft = await seed_mail(app)
        scanned = record_scans(monkeypatch)
        await app.services.email.endorse_style(app.principal, draft.id, draft.revision)
        assert not MAILBOX & set(scanned)


async def test_endorsement_never_learns_an_expired_sent_body() -> None:
    """A sent draft past the window has no wording left to endorse, swept or not."""
    async with email_client() as (app, _):
        _, draft = await seed_mail(app)
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            sent = draft.model_copy(
                update={
                    "status": EmailDraftStatus.SENT,
                    "updated_at": app.clock.now() - timedelta(days=31),
                }
            )
            await save_value(
                uow.email, app.principal, "draft", str(draft.id), sent, sent.updated_at
            )
        with pytest.raises(ValueError, match="needs wording"):
            await app.services.email.endorse_style(app.principal, draft.id, draft.revision)
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "style") == []


async def test_inbox_listing_never_validates_message_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing needs summaries only; bodies of rows it cannot return are never parsed."""
    async with email_client() as (app, _):
        priority, _ = await seed_mail(app)
        await seed_mail(app, priority=0.2)
        validated: list[bool] = []
        original = EmailThread.model_validate.__func__  # type: ignore[attr-defined]

        def recording(cls: type[EmailThread], obj: Any, *args: Any, **kwargs: Any) -> EmailThread:
            validated.append(bool(isinstance(obj, dict) and obj.get("messages")))
            return cast(EmailThread, original(cls, obj, *args, **kwargs))

        monkeypatch.setattr(EmailThread, "model_validate", classmethod(recording))
        page = await app.services.email.threads(app.principal)
        items = cast(list[dict[str, object]], page["items"])
        assert [item["id"] for item in items] == [str(priority.id)]
        assert validated and not any(validated)


async def test_task_point_reads_withhold_expired_bodies() -> None:
    """Worker reads of one conversation apply the thirty-day rule without maintenance."""
    async with email_client() as (app, _):
        thread, _ = await seed_mail(app)
        await expire(app, thread.id)
        async with app.uow_factory() as uow:
            cached = await app.services.email.thread_record(uow.email, app.principal, thread.id)
        assert [message.body for message in cached.messages] == [""]
        assert [message.id for message in cached.messages] == ["m1"]
        assert cached.complete is False


async def test_draft_generation_never_receives_an_expired_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[ScriptedTurn(text='{"body": "Reply"}')]),
        mcp_client_factory=await _current_mail_factory(),
    ) as app:
        thread, _ = await _seed_draft(app)
        await expire(app, thread.id)
        requests = record_model_requests(monkeypatch)
        operation = await app.services.email.submit_task(
            app.principal, kind="draft", thread_id=thread.id
        )
        await app.runs.get(operation.run_id)
        assert not any(PROVIDER_BODY in request for request in requests)


async def test_refresh_refetches_an_expired_body_before_maintenance_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unswept expired body is neither assessed nor kept; the provider copy replaces it."""
    stale = "Stale cached wording from last quarter."
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[_assessment_turn(), _assessment_turn()]),
        mcp_client_factory=await _current_mail_factory(),
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "thread")
        thread = EmailThread.model_validate(row.payload)
        await expire(
            app,
            thread.id,
            messages=[message.model_copy(update={"body": stale}) for message in thread.messages],
            assessment_version="",
        )
        requests = record_model_requests(monkeypatch)
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(second.run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            [row] = await uow.email.list(app.principal, "thread")
        refreshed = EmailThread.model_validate(row.payload)
        assert [message.body for message in refreshed.messages] == [PROVIDER_BODY]
        assert refreshed.last_accessed_at > app.clock.now() - timedelta(days=1)
        assert not any(stale in request for request in requests)


async def test_email_cache_sweep_runs_immediately_then_on_its_own_interval(
    tmp_path: Path,
) -> None:
    clock = FixedClock(datetime(2026, 9, 16, 12, tzinfo=UTC))
    sweeps = 0

    async def sweep_email_cache() -> int:
        nonlocal sweeps
        sweeps += 1
        return 0

    async with build(settings=_settings(tmp_path), script=_script(), clock=clock) as app:
        worker = MaintenanceWorker(
            uow_factory=app.uow_factory,
            clock=clock,
            sweep_email_cache=sweep_email_cache,
            email_cache_sweep_interval_seconds=600,
        )
        await worker.run_once()
        await worker.run_once()
        assert sweeps == 1
        clock.advance(timedelta(seconds=601))
        await worker.run_once()

    assert sweeps == 2


async def test_composed_maintenance_sweeps_the_email_cache_hourly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readers enforce the thirty-day window, so the sweep only reclaims stored bodies."""
    clock = FixedClock(datetime(2026, 9, 16, 12, tzinfo=UTC))
    sweeps: list[str] = []

    async def sweep(principal: Principal) -> int:
        sweeps.append(principal.principal_id)
        return 0

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), clock=clock
    ) as app:
        monkeypatch.setattr(app.services.email, "expire_cache", sweep)
        worker = cast(MaintenanceWorker, app.maintenance_factory())
        await worker.run_once()
        clock.advance(timedelta(minutes=59))
        await worker.run_once()
        assert sweeps == [app.principal.principal_id]
        clock.advance(timedelta(minutes=1))
        await worker.run_once()
        assert len(sweeps) == 2
