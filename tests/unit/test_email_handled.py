"""Revision-scoped handled state stays separate from mail and learned preferences."""

import pytest

from agent_core.application.email import save_value
from tests.gates.test_email_experience_m26 import email_client, seed_mail


async def test_handled_can_be_undone_without_changing_mail_feedback_or_drafts() -> None:
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        path = f"/v1/email/threads/{thread.id}"
        async with app.uow_factory() as uow:
            before = await uow.email.list(app.principal, "draft")
        for dismissed in (True, True, False, False):
            response = await client.post(
                f"{path}/dismiss", json={"expected_revision": 1, "dismissed": dismissed}
            )
            assert response.status_code == 200
            detail = (await client.get(path)).json()
            assert detail["dismissed_revision"] == (1 if dismissed else None)
            assert detail["revision"] == 1
            assert detail["needs_reply"] is True
            assert detail["priority"] == thread.priority
            inbox = (await client.get("/v1/email/threads")).json()["items"]
            assert bool(inbox) is not dismissed
            other = (await client.get("/v1/email/threads?view=other")).json()["items"]
            assert bool(other) is dismissed
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "feedback") == []
            assert await uow.email.list(app.principal, "task") == []
            assert await uow.email.list(app.principal, "draft") == before


async def test_new_source_revision_reopens_handled_mail_and_rejects_stale_actions() -> None:
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        path = f"/v1/email/threads/{thread.id}/dismiss"
        assert (await client.post(path, json={"expected_revision": 1})).status_code == 200
        updated = thread.model_copy(update={"revision": 2, "dismissed_revision": 1})
        async with app.uow_factory() as uow:
            await save_value(
                uow.email, app.principal, "thread", str(thread.id), updated, app.clock.now()
            )
        [reopened] = (await client.get("/v1/email/threads")).json()["items"]
        assert reopened["revision"] == 2
        for dismissed in (True, False):
            response = await client.post(
                path, json={"expected_revision": 1, "dismissed": dismissed}
            )
            assert response.status_code == 409
        [still_open] = (await client.get("/v1/email/threads")).json()["items"]
        assert still_open == reopened


@pytest.mark.parametrize("value", ["false", 0, None, [], {}])
async def test_handled_requires_a_boolean(value: object) -> None:
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        response = await client.post(
            f"/v1/email/threads/{thread.id}/dismiss",
            json={"expected_revision": 1, "dismissed": value},
        )
        assert response.status_code == 400
        assert len((await client.get("/v1/email/threads")).json()["items"]) == 1
