"""Milestone 26 application boundaries and personalization contracts."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.email import (
    EmailDraft,
    EmailDraftStatus,
    EmailMessage,
    EmailRecord,
    EmailThread,
)
from agent_core.domain.errors import AuthorizationError
from agent_core.domain.events import NewEvent
from agent_core.domain.runs import RunStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.worker import MaintenanceWorker
from tests.integration.m2_support import memory_settings


def test_email_experience_has_independent_default_off_flag_and_exact_scopes() -> None:
    environment = {
        "DATABASE_URL": "postgresql+asyncpg://127.0.0.1:1/unused",
        "DEPLOYMENT_MODE": "development",
        "AUTH_MODE": "dev",
        "AGENT_SANDBOX": "fake",
    }
    disabled = load_settings(environment)
    assert getattr(disabled, "email_mode_enabled", False) is False
    enabled = load_settings({**environment, "AGENT_EMAIL_MODE_ENABLED": "1"})
    assert getattr(enabled, "email_mode_enabled", False) is True
    assert {"email.read", "email.write"} <= PLATFORM_SCOPES


async def test_email_api_is_flag_gated_scope_checked_and_private() -> None:
    for enabled, scopes, expected in (
        (False, {"email.read"}, 404),
        (True, set(), 403),
        (True, {"email.read"}, 200),
    ):
        principal = Principal(tenant_id="local", principal_id="owner", scopes=scopes)
        async with build(
            settings=replace(memory_settings(), email_mode_enabled=enabled),
            storage="memory",
            principal=principal,
        ) as composition:
            app = create_app(
                composition.services,
                composition.settings,
                composition.principal,
                composition.new_request_id,
                composition.readiness_probe,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
            ) as client:
                response = await client.get("/v1/email/accounts")
                assert response.status_code == expected
                if enabled:
                    assert response.headers.get("cache-control") == "private, no-store"
                if expected == 200:
                    assert response.json() == {"items": [], "next_cursor": None}
                    assert response.headers["cache-control"] == "private, no-store"


@asynccontextmanager
async def email_client() -> AsyncIterator[tuple[Composition, httpx.AsyncClient]]:
    principal = Principal(
        tenant_id="local",
        principal_id="owner",
        roles={"user"},
        scopes={
            "email.read",
            "email.write",
            "run.read",
            "run.write",
            "session.read",
            "session.write",
            "mcp.gmail_read.use",
            "mcp.gmail_send.use",
        },
    )
    async with build(
        settings=replace(memory_settings(), email_mode_enabled=True),
        storage="memory",
        principal=principal,
    ) as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
        ) as client:
            yield composition, client


async def seed_mail(
    composition: Composition, *, priority: float = 0.8
) -> tuple[EmailThread, EmailDraft]:
    composition.services.email.account_ids = ("work",)
    composition.services.email.account_servers = {
        "work": {"read": "gmail_read", "send": "gmail_send"}
    }
    now = datetime(2026, 9, 11, tzinfo=UTC)
    thread = EmailThread(
        id=uuid4(),
        account_id="work",
        provider_thread_id="gmail-thread",
        subject="Board materials",
        updated_at=now,
        last_accessed_at=now,
        senders=["ceo@example.com"],
        priority=priority,
        summary="Review the board materials",
        reason="Direct request",
        complete=True,
        needs_reply=True,
        messages=[
            EmailMessage(
                id="m1",
                sender="ceo@example.com",
                to=["owner@example.com"],
                sent_at=now,
                body="Please review the board materials.",
                complete=True,
            )
        ],
    )
    draft = EmailDraft(
        id=uuid4(),
        thread_id=thread.id,
        account_id="work",
        source_revision=1,
        to=["ceo@example.com"],
        subject="Re: Board materials",
        body="Thanks. I'll take a look.",
        updated_at=now,
    )
    thread = thread.model_copy(update={"draft_id": draft.id})
    async with composition.uow_factory() as uow:
        for kind, value in (("thread", thread), ("draft", draft)):
            await uow.email.put(
                EmailRecord(
                    tenant_id="local",
                    principal_id="owner",
                    kind=kind,
                    key=str(value.id),
                    revision=1,
                    payload=value.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                ),
                expected_revision=0,
            )
    return thread, draft


async def test_priority_selection_feedback_scope_and_undo() -> None:
    async with email_client() as (composition, client):
        first, _ = await seed_mail(composition)
        second, _ = await seed_mail(composition)
        inbox = await client.get("/v1/email/threads")
        assert inbox.status_code == 200
        assert len(inbox.json()["items"]) == 2
        changed = await client.post(
            "/v1/email/feedback",
            json={
                "thread_id": str(first.id),
                "target": "thread",
                "judgment": "less_important",
            },
            headers={"Idempotency-Key": "feedback-1"},
        )
        assert changed.status_code == 200
        feedback_id = changed.json()["feedback_id"]
        selected = (await client.get("/v1/email/threads")).json()["items"]
        assert [item["id"] for item in selected] == [str(second.id)]
        replay = await client.post(
            "/v1/email/feedback",
            json={
                "thread_id": str(first.id),
                "target": "thread",
                "judgment": "less_important",
            },
            headers={"Idempotency-Key": "feedback-1"},
        )
        assert replay.json()["feedback_id"] == feedback_id
        undone = await client.delete(f"/v1/email/feedback/{feedback_id}")
        assert undone.status_code == 200
        assert undone.json()["id"] == str(first.id)
        assert len((await client.get("/v1/email/threads")).json()["items"]) == 2


async def test_draft_edits_preserve_revisions_and_reject_stale_writes() -> None:
    async with email_client() as (composition, client):
        _, draft = await seed_mail(composition)
        update = {
            "expected_revision": 1,
            "to": ["ceo@example.com"],
            "cc": [],
            "bcc": [],
            "subject": draft.subject,
            "body": "Thanks — I'll review before our meeting.",
        }
        result = await client.put(f"/v1/email/drafts/{draft.id}", json=update)
        assert result.status_code == 200
        assert result.json()["revision"] == 2
        conflict = await client.put(
            f"/v1/email/drafts/{draft.id}", json={**update, "body": "Other edit"}
        )
        assert conflict.status_code == 409
        current = await client.get(f"/v1/email/drafts/{draft.id}")
        assert current.json()["body"] == update["body"]
        history = await client.get(f"/v1/email/drafts/{draft.id}/revisions")
        assert {row["body"] for row in history.json()["items"]} >= {
            draft.body,
            update["body"],
            "Other edit",
        }


async def test_cross_owner_email_id_is_indistinguishable_from_absence() -> None:
    async with email_client() as (composition, client):
        thread, draft = await seed_mail(composition)
        async with composition.uow_factory() as uow:
            for kind, value in (("thread", thread), ("draft", draft)):
                row = await uow.email.get(composition.principal, kind, str(value.id))
                assert row is not None
                await uow.email.delete(composition.principal, kind, row.key, expected_revision=1)
                assert row is not None
                await uow.email.put(
                    row.model_copy(update={"principal_id": "someone-else"}), expected_revision=0
                )
        assert (await client.get(f"/v1/email/threads/{thread.id}")).status_code == 404
        assert (await client.get(f"/v1/email/drafts/{draft.id}")).status_code == 404
        assert (await client.get("/v1/email/threads")).json()["items"] == []


async def assert_refresh_admits_one_durable_typed_task_without_owner_message() -> None:
    async with email_client() as (composition, client):
        calls = []

        async def dispatch(run_id: UUID) -> None:
            calls.append(run_id)

        service = composition.services.email
        service.account_ids = ("work",)
        service.account_servers = {"work": {"read": "gmail_read", "send": "gmail_send"}}
        service.dispatch = dispatch
        first = await client.post("/v1/email/refresh", headers={"Idempotency-Key": "refresh-a"})
        assert first.status_code == 200
        second = await client.post("/v1/email/refresh", headers={"Idempotency-Key": "refresh-b"})
        assert second.status_code == 200
        assert first.json()["run_id"] == second.json()["run_id"]
        assert second.json()["replayed"] is True
        assert len(calls) == 1
        async with composition.uow_factory() as uow:
            jobs = await uow.email.list(composition.principal, "task")
            assert len(jobs) == 1
            assert jobs[0].payload["reservation"] == "1"
            run = await uow.runs.get(calls[0], composition.principal)
            history = await uow.history.catch_up(run.session_id)
            assert history.items == []
            assert run.limits.deadline_at is not None
            assert run.limits.max_cost == 1
        sessions = await client.get("/v1/sessions")
        assert sessions.json()["items"] == []


async def test_learning_pause_and_reset_are_shared_and_keep_saved_mail() -> None:
    async with email_client() as (composition, client):
        thread, _ = await seed_mail(composition)
        initial = await client.get("/v1/email/learning")
        assert initial.status_code == 200
        assert initial.json()["paused"] is False
        paused = await client.put("/v1/email/learning", json={"paused": True})
        assert paused.status_code == 200
        assert (await client.get("/v1/email/learning")).json()["paused"] is True
        reset = await client.post("/v1/email/learning/reset", json={"scope": "all"})
        assert reset.status_code == 200
        assert reset.json()["profile_revision"] > initial.json()["profile_revision"]
        assert (await client.get(f"/v1/email/threads/{thread.id}")).status_code == 200


async def test_cached_email_rechecks_account_permission_after_revocation() -> None:
    async with email_client() as (composition, _):
        thread, _ = await seed_mail(composition)
        composition.services.email.account_servers = {
            "work": {"read": "gmail_read", "send": "gmail_send"}
        }
        reduced = composition.principal.model_copy(update={"scopes": {"email.read"}})
        with pytest.raises(AuthorizationError):
            await composition.services.email.thread(reduced, thread.id)


async def test_retry_after_lost_draft_save_returns_same_revision() -> None:
    async with email_client() as (composition, client):
        _, draft = await seed_mail(composition)
        data = {
            "expected_revision": 1,
            "to": draft.to,
            "subject": draft.subject,
            "body": "My revised response.",
        }
        first = await client.put(
            f"/v1/email/drafts/{draft.id}", json=data, headers={"Idempotency-Key": "edit-1"}
        )
        replay = await client.put(
            f"/v1/email/drafts/{draft.id}", json=data, headers={"Idempotency-Key": "edit-1"}
        )
        assert first.status_code == replay.status_code == 200
        assert first.json()["revision"] == replay.json()["revision"] == 2


async def test_crashed_model_attempt_keeps_automatic_budget_reserved() -> None:
    async with email_client() as (composition, _):
        service = composition.services.email
        service.account_ids = ("work",)
        service.account_servers = {"work": {"read": "gmail_read", "send": "gmail_send"}}

        async def leave_queued(run_id: UUID) -> None:
            pass

        service.dispatch = leave_queued
        operation = await service.submit_task(composition.principal, kind="refresh")
        async with composition.uow_factory() as uow:
            run = await uow.runs.get(operation.run_id, composition.principal)
            await uow.events.append(
                NewEvent(
                    session_id=run.session_id,
                    run_id=run.id,
                    event_type="model.request.started",
                    actor_type="runtime",
                    payload={"attempt_id": str(uuid4())},
                )
            )
            await uow.runs.transition(run.id, RunStatus.QUEUED, RunStatus.RUNNING)
            await uow.runs.transition(run.id, RunStatus.RUNNING, RunStatus.FAILED)
        await service.settle(composition.principal, operation.run_id)
        task = await service.get_task(composition.principal, operation.run_id)
        assert task is not None
        assert task.settled_cost is None
        assert task.reservation == 1


async def test_maintenance_expires_body_cache_and_sent_drafts_but_keeps_unsent_edits() -> None:
    async with email_client() as (composition, _):
        thread, draft = await seed_mail(composition)
        _, unsent = await seed_mail(composition)
        old = datetime(2024, 1, 1, tzinfo=UTC)
        async with composition.uow_factory() as uow:
            for kind, value in (
                ("thread", thread.model_copy(update={"last_accessed_at": old})),
                (
                    "draft",
                    draft.model_copy(update={"updated_at": old, "status": EmailDraftStatus.SENT}),
                ),
            ):
                row = await uow.email.get(composition.principal, kind, str(value.id))
                assert row is not None
                await uow.email.put(
                    row.model_copy(
                        update={"revision": 2, "payload": value.model_dump(mode="json")}
                    ),
                    expected_revision=1,
                )
        await cast(MaintenanceWorker, composition.maintenance_factory()).run_once()
        async with composition.uow_factory() as uow:
            expired_thread = await uow.email.get(composition.principal, "thread", str(thread.id))
            expired_draft = await uow.email.get(composition.principal, "draft", str(draft.id))
            kept = await uow.email.get(composition.principal, "draft", str(unsent.id))
        assert expired_thread is not None and expired_draft is not None and kept is not None
        assert cast(list[dict[str, Any]], expired_thread.payload["messages"])[0]["body"] == ""
        assert expired_draft.payload["body"] == ""
        assert kept.payload["body"] == unsent.body


async def test_source_exclusion_erases_local_derivatives_and_is_replay_safe() -> None:
    async with email_client() as (composition, client):
        thread, draft = await seed_mail(composition)
        other, _ = await seed_mail(composition)
        result = await client.post(
            f"/v1/email/threads/{thread.id}/exclude", json={"expected_revision": 1}
        )
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "erased"
        assert (await client.get(f"/v1/email/threads/{thread.id}")).status_code == 404
        assert (await client.get(f"/v1/email/drafts/{draft.id}")).status_code == 404
        assert (await client.get(f"/v1/email/threads/{other.id}")).status_code == 200
        replay = await client.post(
            f"/v1/email/threads/{thread.id}/exclude", json={"expected_revision": 1}
        )
        assert replay.status_code == 200
        assert replay.json()["source_id"] == result.json()["source_id"]


async def test_discuss_in_chat_carries_typed_reference_without_fabricating_owner_text() -> None:
    async with email_client() as (composition, client):
        thread, _ = await seed_mail(composition)
        result = await client.post(f"/v1/email/threads/{thread.id}/discussion")
        assert result.status_code == 200
        from uuid import UUID

        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(
                UUID(result.json()["session_id"]), composition.principal
            )
            events = await uow.events.list_after(session.id, 0, composition.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)
        assert not any(event.event_type == "user.message.created" for event in events)
        state = [event for event in events if event.event_type == "context.working_state.updated"]
        assert state
        assert str(thread.id) in str(state[0].payload)
        assert {"email.context", "email.feedback"} <= set(agent.enabled_tools)


async def test_direct_draft_history_read_expires_old_sent_bodies() -> None:
    from agent_core.application.email import save_value

    async with email_client() as (composition, client):
        _, draft = await seed_mail(composition)
        service = composition.services.email
        old = draft.model_copy(
            update={"status": EmailDraftStatus.SENT, "updated_at": datetime(2024, 1, 1, tzinfo=UTC)}
        )
        async with composition.uow_factory() as uow, uow.email.lock(composition.principal):
            await save_value(
                uow.email, composition.principal, "draft", str(old.id), old, service.clock.now()
            )
            await service._archive_draft(uow.email, composition.principal, old)
        result = await client.get(f"/v1/email/drafts/{draft.id}/revisions")
        assert result.status_code == 200
        assert not any(item["body"] for item in result.json()["items"])


@pytest.mark.parametrize(
    ("cost", "age", "settled", "expected"),
    [
        ("20", 0, True, 402),
        ("200", 2, True, 402),
        ("200", 31, True, 200),
        ("19", 0, True, 200),
        ("20", 31, False, 402),
    ],
)
async def test_aggregate_email_ceilings_cover_accounts_and_unresolved_reservations(
    cost: str,
    age: int,
    settled: bool,
    expected: int,
) -> None:
    from datetime import timedelta
    from decimal import Decimal

    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailTask

    async with email_client() as (composition, client):
        service = composition.services.email
        service.account_ids = ("work",)
        service.account_servers = {"work": {"read": "gmail_read", "send": "gmail_send"}}

        async def dispatch(run_id: UUID) -> None:
            pass

        service.dispatch = dispatch
        prior = EmailTask(
            id=uuid4(),
            run_id=uuid4(),
            session_id=uuid4(),
            kind="draft",
            account_ids=["personal"],
            created_at=service.clock.now() - timedelta(days=age),
            reservation=Decimal(cost),
            settled_cost=Decimal(cost) if settled else None,
        )
        async with composition.uow_factory() as uow, uow.email.lock(composition.principal):
            await save_value(
                uow.email,
                composition.principal,
                "task",
                str(prior.run_id),
                prior,
                service.clock.now(),
            )
        response = await client.post("/v1/email/refresh")
        assert response.status_code == expected, response.text


async def test_refresh_continues_with_remaining_authorized_account() -> None:
    async with email_client() as (composition, client):
        service = composition.services.email
        service.account_ids = ("work", "personal")
        service.account_servers = {
            "work": {"read": "gmail_read", "send": "gmail_send"},
            "personal": {"read": "gmail_personal_read", "send": "gmail_personal_send"},
        }

        async def dispatch(run_id: UUID) -> None:
            pass

        service.dispatch = dispatch
        result = await client.post("/v1/email/refresh")
        assert result.status_code == 200
        task = await service.get_task(composition.principal, UUID(result.json()["run_id"]))
        assert task is not None and task.account_ids == ["work"]


async def test_account_capability_rebinding_preserves_old_evidence_and_new_operations() -> None:
    async with email_client() as (composition, _):
        thread, _ = await seed_mail(composition)
        service = composition.services.email
        initial = await service.discussion(composition.principal, thread.id)
        service.account_servers = {"work": {"read": "gmail_work_read", "send": "gmail_work_send"}}
        updated_principal = composition.principal.model_copy(
            update={
                "scopes": {
                    *composition.principal.scopes,
                    "mcp.gmail_work_read.use",
                    "mcp.gmail_work_send.use",
                }
            }
        )
        rebound = await service.discussion(updated_principal, thread.id)
        assert rebound["session_id"] != initial["session_id"]
        async with composition.uow_factory() as uow:
            old = await uow.sessions.get(UUID(str(initial["session_id"])), updated_principal)
            new = await uow.sessions.get(UUID(str(rebound["session_id"])), updated_principal)
        assert old.metadata["email_account_servers"]["work"]["read"] == "gmail_read"
        assert new.metadata["email_account_servers"]["work"]["read"] == "gmail_work_read"
        assert (await service.thread(updated_principal, thread.id))["id"] == str(thread.id)


async def test_refresh_admits_one_durable_typed_task_without_owner_message() -> None:
    await assert_refresh_admits_one_durable_typed_task_without_owner_message()
