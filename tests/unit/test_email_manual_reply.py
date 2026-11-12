"""Reply opens owner composition without generating text; variants survive switching."""

from uuid import uuid4

import pytest

from agent_core.application.email import save_value
from agent_core.domain.email import EmailDraftStatus
from tests.gates.test_email_experience_m26 import email_client
from tests.gates.test_email_learning_m26 import observation, prepare


async def test_reply_creates_blank_verified_draft_without_a_run_and_replays() -> None:
    async with email_client() as (app, client):
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        path = f"/v1/email/threads/{thread.id}/reply"
        body = {"kind": "manual", "source_revision": thread.revision}
        response = await client.post(path, json=body, headers={"Idempotency-Key": "reply"})
        assert response.status_code == 200, response.text
        draft = response.json()
        assert draft["body"] == ""
        assert draft["composition"] == "manual"
        assert draft["to"] == ["reply@example.com"]
        assert draft["cc"] == ["colleague@example.com"]
        assert draft["in_reply_to"] == "<m1@example.com>"
        assert draft["run_id"] is None
        replay = await client.post(path, json=body, headers={"Idempotency-Key": "reply"})
        assert replay.json() == draft
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "task") == []


async def test_reply_keeps_manual_and_veetbot_text_as_revisions_of_one_draft() -> None:
    async with email_client() as (app, client):
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        ai = await service.save_generated_draft(
            app.principal, thread.id, thread.revision, "AI suggestion", run_id=uuid4()
        )
        path = f"/v1/email/threads/{thread.id}/reply"
        response = await client.post(
            path,
            json={
                "kind": "manual",
                "source_revision": thread.revision,
                "expected_revision": ai.revision,
            },
        )
        assert response.status_code == 200, response.text
        manual = response.json()
        assert manual["id"] == str(ai.id)
        assert manual["body"] == ""
        saved = await client.put(
            f"/v1/email/drafts/{ai.id}",
            json={
                "expected_revision": manual["revision"],
                "to": manual["to"],
                "subject": manual["subject"],
                "body": "My own words",
            },
        )
        assert saved.status_code == 200
        selected = await client.post(
            path,
            json={
                "kind": "veetbot",
                "source_revision": thread.revision,
                "expected_revision": saved.json()["revision"],
            },
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["body"] == "AI suggestion"
        restored = await client.post(
            path,
            json={
                "kind": "manual",
                "source_revision": thread.revision,
                "expected_revision": selected.json()["revision"],
            },
        )
        assert restored.status_code == 200
        assert restored.json()["body"] == "My own words"
        assert restored.json()["composition"] == "manual"


@pytest.mark.parametrize(
    "failure", ["source", "target", "account", "scope", "read_scope", "revision"]
)
async def test_reply_rejects_unverified_or_unauthorized_state(failure: str) -> None:
    async with email_client() as (app, client):
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        if failure == "target":
            thread = thread.model_copy(
                update={
                    "messages": [thread.messages[-1].model_copy(update={"message_id_header": None})]
                }
            )
            async with app.uow_factory() as uow:
                await save_value(
                    uow.email, app.principal, "thread", str(thread.id), thread, app.clock.now()
                )
        if failure == "account":
            app.principal.scopes.discard("mcp.gmail_read.use")
        if failure in {"scope", "read_scope"}:
            app.principal.scopes.discard("email.write" if failure == "scope" else "email.read")
        response = await client.post(
            f"/v1/email/threads/{thread.id}/reply",
            json={
                "kind": "manual",
                "source_revision": thread.revision + (failure == "source"),
                "expected_revision": 9 if failure == "revision" else None,
            },
        )
        assert response.status_code in (
            {403, 404} if failure in {"scope", "read_scope", "account"} else {409}
        )
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "draft") == []


async def test_reply_validates_kind_and_refuses_missing_ai_proposal() -> None:
    async with email_client() as (app, client):
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        path = f"/v1/email/threads/{thread.id}/reply"
        assert (
            await client.post(path, json={"kind": "invented", "source_revision": 1})
        ).status_code == 400
        assert (
            await client.post(path, json={"kind": "veetbot", "source_revision": 1})
        ).status_code == 404


async def test_reply_rejects_changed_retry_and_preserves_a_send_in_progress() -> None:
    async with email_client() as (app, client):
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        path = f"/v1/email/threads/{thread.id}/reply"
        body = {"kind": "manual", "source_revision": thread.revision}
        response = await client.post(path, json=body, headers={"Idempotency-Key": "same"})
        assert response.status_code == 200
        changed = await client.post(
            path, json={**body, "kind": "veetbot"}, headers={"Idempotency-Key": "same"}
        )
        assert changed.status_code == 409
        from uuid import UUID

        draft = await service.draft(app.principal, UUID(response.json()["id"]))
        sending = draft.model_copy(update={"status": EmailDraftStatus.SENDING})
        async with app.uow_factory() as uow:
            await save_value(
                uow.email, app.principal, "draft", str(draft.id), sending, app.clock.now()
            )
        refused = await client.post(path, json={**body, "expected_revision": draft.revision})
        assert refused.status_code == 409
        assert (await service.draft(app.principal, draft.id)).status == "sending"


@pytest.mark.parametrize("status", [EmailDraftStatus.SENT, EmailDraftStatus.DISCARDED])
async def test_reply_starts_fresh_after_inactive_stale_composition(
    status: EmailDraftStatus,
) -> None:
    async with email_client() as (app, client):
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        old = await service.save_generated_draft(
            app.principal, thread.id, thread.revision, "Old reply", run_id=uuid4()
        )
        async with app.uow_factory() as uow:
            await save_value(
                uow.email,
                app.principal,
                "draft",
                str(old.id),
                old.model_copy(update={"status": status, "stale": True}),
                app.clock.now(),
            )
        response = await client.post(
            f"/v1/email/threads/{thread.id}/reply",
            json={
                "kind": "manual",
                "source_revision": thread.revision,
                "expected_revision": old.revision,
            },
        )
        assert response.status_code == 200, response.text
        fresh = response.json()
        assert fresh["id"] != str(old.id)
        assert fresh["body"] == "" and fresh["status"] == "ready" and not fresh["stale"]
        assert (await service.draft(app.principal, old.id)).status == status
