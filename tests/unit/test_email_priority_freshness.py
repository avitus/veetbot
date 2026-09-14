"""Current attention expires independently of historical mailbox coverage."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_experience_m26 import email_client
from tests.gates.test_email_learning_m26 import observation, prepare
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _assessment_turn, _current_mail_factory


def assessment(expires_at: str | None) -> dict[str, Any]:
    """Build grounded importance features with optional attention expiry overrides."""
    return {
        "summary": "Meeting invitation",
        "reason": "Please confirm attendance",
        "topics": ["meeting"],
        "content_importance": 1,
        "relationship_importance": 0,
        "urgency": 1,
        "needs_reply": True,
        "profile_revision": 1,
        "attention_expires_at": expires_at,
    }


@pytest.mark.parametrize("expired_on_arrival", [False, True])
@pytest.mark.parametrize("needs_reply", [False, True])
async def test_meeting_attention_expires_without_mailbox_changes(
    expired_on_arrival: bool,
    needs_reply: bool,
) -> None:
    """Expire time-bounded attention during projection without changing the source mailbox."""
    async with email_client() as (app, client):
        service = await prepare(app)
        clock = FixedClock(datetime(2026, 9, 13, 17, tzinfo=UTC))
        service.clock = clock
        source = observation(body="Please confirm attendance for September 13 at 10am PDT.")
        source["messages"][0]["internal_date"] = str(
            int(datetime(2026, 8, 28, 12, tzinfo=UTC).timestamp() * 1000)
        )
        thread = await service.import_thread(app.principal, "work", source, uuid4())
        assert thread.updated_at < clock.now() - timedelta(days=14)
        expiry = clock.now() + timedelta(seconds=1)
        if expired_on_arrival:
            clock.advance(timedelta(seconds=1))
        value = assessment(expiry.isoformat())
        value["needs_reply"] = needs_reply
        await service.save_assessment(app.principal, thread.id, thread.revision, value)
        if not expired_on_arrival:
            before = await client.get("/v1/email/threads")
            assert [item["id"] for item in before.json()["items"]] == [str(thread.id)]
            clock.advance(timedelta(seconds=1))
        priority = await client.get("/v1/email/threads")
        assert priority.status_code == 200
        assert priority.json()["items"] == []
        other = await client.get("/v1/email/threads", params={"view": "other"})
        assert [item["id"] for item in other.json()["items"]] == [str(thread.id)]
        detail = await service.thread(app.principal, thread.id)
        assert detail["needs_reply"] is False
        assert detail["revision"] == thread.revision
        assert detail["in_inbox"] is True
        assert detail["messages"]
        # A new source revision can carry a rescheduled meeting; its old expiry
        # cannot suppress the new assessment or be renewed by an unchanged read.
        source["messages"][0]["body"] = "The meeting is now on September 20."
        changed = await service.import_thread(app.principal, "work", source, uuid4())
        assert changed.attention_expires_at is None
        await service.save_assessment(
            app.principal,
            changed.id,
            changed.revision,
            assessment("2026-09-20T10:00:00-07:00"),
        )
        assert (await service.threads(app.principal))["items"]


async def test_old_unresolved_request_remains_eligible_without_event_expiry() -> None:
    """Retain an old unresolved request when no supported attention expiry exists."""
    async with email_client() as (app, _):
        service = await prepare(app)
        service.clock = FixedClock(datetime(2026, 9, 13, tzinfo=UTC))
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        await service.save_assessment(app.principal, thread.id, thread.revision, assessment(None))
        page = await service.threads(app.principal)
        assert isinstance(page["items"], list)
        assert [item["id"] for item in page["items"]] == [str(thread.id)]


async def test_expiry_preserves_explicit_feedback_and_undo_restores_expiry() -> None:
    """Let owner feedback override expiry and restore expiry when that feedback is undone."""
    async with email_client() as (app, _):
        service = await prepare(app)
        service.clock = FixedClock(datetime(2026, 9, 13, tzinfo=UTC))
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        await service.save_assessment(
            app.principal,
            thread.id,
            thread.revision,
            assessment("2026-08-25T10:00:00-07:00"),
        )
        feedback = await service.feedback(
            app.principal, thread_id=thread.id, target="thread", judgment="important"
        )
        assert (await service.threads(app.principal))["items"]
        await service.undo_feedback(app.principal, UUID(str(feedback["feedback_id"])))
        assert (await service.threads(app.principal))["items"] == []


@pytest.mark.parametrize("expiry", ["2026-09-13T10:00:00", "not-a-date", 123])
async def test_malformed_expiry_cannot_mutate_assessment(expiry: object) -> None:
    """Reject malformed or naive expiry values before stored assessment state changes."""
    async with email_client() as (app, _):
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        value = assessment(None)
        value["attention_expires_at"] = expiry
        with pytest.raises(ValueError):
            await service.save_assessment(app.principal, thread.id, thread.revision, value)
        async with app.uow_factory() as uow:
            assert await uow.email.get(app.principal, "assessment", str(thread.id)) is None
            unchanged = await service._thread(uow.email, app.principal, thread.id)
        assert unchanged == thread


async def test_assessment_receives_trusted_current_time_separate_from_message_date() -> None:
    """Supply trusted assessment time separately from the original correspondence date."""
    from dataclasses import replace

    now = datetime(2026, 9, 13, 17, tzinfo=UTC)
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(now),
        mcp_client_factory=await _current_mail_factory(),
        script=FakeModelScript(turns=[_assessment_turn()]),
    ) as app:
        task = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(task.run_id)).status is RunStatus.COMPLETED
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        [request] = provider.requests
        assert f"Current assessment time: {now.isoformat()}" in request.model_dump_json()
        assert "2026-09-11T12:00:00" in request.model_dump_json()


@pytest.mark.parametrize("needs_reply", [False, True])
async def test_expired_model_result_never_surfaces_or_auto_drafts_and_poll_is_cached(
    needs_reply: bool,
) -> None:
    """Suppress expired attention and drafting without repeating unchanged model assessment."""
    import json
    from dataclasses import replace

    value = json.loads(_assessment_turn(needs_reply=needs_reply).text or "{}")
    value["attention_expires_at"] = "2026-09-12T10:00:00-07:00"
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(datetime(2026, 9, 13, 17, tzinfo=UTC)),
        mcp_client_factory=await _current_mail_factory(),
        script=FakeModelScript(turns=[ScriptedTurn(text=json.dumps(value))]),
    ) as app:
        first = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(first.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 1
        assert (await app.services.email.threads(app.principal))["items"] == []
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "draft") == []
        second = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(second.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 0


async def test_legacy_cached_assessment_is_replaced_once_on_foreground_refresh() -> None:
    """Upgrade a legacy assessment once under the current prompt and then reuse it."""
    import json
    from dataclasses import replace

    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailThread

    updated = json.loads(_assessment_turn().text or "{}")
    updated["attention_expires_at"] = "2026-09-12T10:00:00-07:00"
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        clock=FixedClock(datetime(2026, 9, 13, 17, tzinfo=UTC)),
        mcp_client_factory=await _current_mail_factory(),
        script=FakeModelScript(turns=[_assessment_turn(), ScriptedTurn(text=json.dumps(updated))]),
    ) as app:
        service = app.services.email
        first = await service.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(first.run_id)).status is RunStatus.COMPLETED
        assert (await service.threads(app.principal))["items"]
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            [row] = await uow.email.list(app.principal, "assessment")
            legacy = dict(row.payload)
            revision = str(legacy["model_revision"])
            legacy["model_revision"] = revision.rsplit(":", 1)[0] + ":email-assessment@1"
            legacy.pop("attention_expires_at", None)
            await service._put_data(uow.email, app.principal, "assessment", row.key, legacy)
            stored = await service._thread(uow.email, app.principal, UUID(row.key))
            old_payload = stored.model_dump()
            old_payload.pop("attention_expires_at")
            await save_value(
                uow.email,
                app.principal,
                "thread",
                row.key,
                EmailThread.model_validate(old_payload),
                app.clock.now(),
            )
        second = await service.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(second.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 1
        assert (await service.threads(app.principal))["items"] == []
        third = await service.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(third.run_id)).model_call_count == 0
