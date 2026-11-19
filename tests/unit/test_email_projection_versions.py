"""Refresh validators cover current authorized projections, never just the first page."""

from datetime import timedelta

import pytest

from agent_core.application.email import save_value
from agent_core.application.email_reader import reading_body
from agent_core.domain.email import EmailThread
from tests.gates.test_email_experience_m26 import email_client, seed_mail


async def test_list_snapshot_covers_later_pages_and_ignores_access_time() -> None:
    async with email_client() as (app, client):
        first, _ = await seed_mail(app)
        later, _ = await seed_mail(app, priority=0.75)
        path = "/v1/email/threads"
        initial = (await client.get(path, params={"limit": 1})).json()
        version = initial.get("snapshot_version")
        assert version, "lists need a complete snapshot version"
        second = (await client.get(path, params={"limit": 1, "cursor": "1"})).json()
        assert second["snapshot_version"] == version
        await client.get(f"{path}/{first.id}")
        assert (await client.get(path)).json()["snapshot_version"] == version
        async with app.uow_factory() as uow:
            changed = later.model_copy(update={"summary": "Changed on page two"})
            await save_value(
                uow.email, app.principal, "thread", str(later.id), changed, app.clock.now()
            )
        updated = (await client.get(path, params={"limit": 1})).json()
        assert updated["items"] == initial["items"]
        assert updated["snapshot_version"] != version


@pytest.mark.parametrize("change", ["draft", "body", "retention", "permission"])
async def test_thread_validator_checks_current_state_before_reuse(change: str) -> None:
    async with email_client() as (app, client):
        thread, draft = await seed_mail(app)
        path = f"/v1/email/threads/{thread.id}"
        first = await client.get(path)
        tag = first.headers.get("etag")
        assert tag, "thread reads need a retained-content validator"
        unchanged = await client.get(path, headers={"If-None-Match": tag})
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        assert unchanged.headers["cache-control"] == "private, no-store"
        if change == "permission":
            app.services.email.account_servers = {}
        else:
            async with app.uow_factory() as uow:
                if change == "draft":
                    value = draft.model_copy(update={"body": "An edit from another device"})
                    await save_value(
                        uow.email, app.principal, "draft", str(draft.id), value, app.clock.now()
                    )
                else:
                    row = await uow.email.get(app.principal, "thread", str(thread.id))
                    assert row is not None
                    current = EmailThread.model_validate(row.payload)
                    if change == "retention":
                        current = current.model_copy(
                            update={"last_accessed_at": app.clock.now() - timedelta(days=31)}
                        )
                    else:
                        message = current.messages[0].model_copy(
                            update={"body": "Different retained content"}
                        )
                        current = current.model_copy(update={"messages": [message]})
                    await save_value(
                        uow.email, app.principal, "thread", str(thread.id), current, app.clock.now()
                    )
        response = await client.get(path, headers={"If-None-Match": tag})
        assert response.status_code == (404 if change == "permission" else 200)
        if change != "permission":
            assert response.headers["etag"] != tag


async def test_matching_thread_validator_skips_reader_work(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_core.api.email as api

    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        path = f"/v1/email/threads/{thread.id}"
        first = await client.get(path)
        tag = first.headers.get("etag")
        assert tag, "thread reads need a validator before reader reuse"

        def unexpected(*args: object, **kwargs: object) -> str:
            pytest.fail("unchanged thread must not rebuild reading text")

        monkeypatch.setattr(api, "reading_body", unexpected)
        assert (await client.get(path, headers={"If-None-Match": tag})).status_code == 304


async def test_reader_preparation_does_not_run_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import agent_core.api.email as api

    event_thread = threading.get_ident()
    original = reading_body
    workers: list[int] = []

    def recording(*args: object, **kwargs: object) -> str:
        workers.append(threading.get_ident())
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(api, "reading_body", recording)
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        assert (await client.get(f"/v1/email/threads/{thread.id}")).status_code == 200
    assert workers and event_thread not in workers


@pytest.mark.parametrize("change", ["feedback", "expiry", "erasure", "account", "scope", "search"])
async def test_list_snapshot_observes_filter_and_attention_changes(change: str) -> None:
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        path = "/v1/email/threads"
        before = (await client.get(path)).json()["snapshot_version"]
        params: dict[str, str] = {}
        if change == "feedback":
            response = await client.post(
                "/v1/email/feedback",
                json={
                    "thread_id": str(thread.id),
                    "target": "thread",
                    "judgment": "less_important",
                    "expected_revision": 1,
                },
                headers={"Idempotency-Key": "snapshot-feedback"},
            )
            assert response.status_code == 200
        elif change == "scope":
            app.principal.scopes.discard("mcp.gmail_read.use")
        elif change == "account":
            params = {"account_id": "missing"}
        elif change == "search":
            params = {"text": "no matching subject"}
        else:
            async with app.uow_factory() as uow:
                if change == "erasure":
                    await uow.email.delete(
                        app.principal, "thread", str(thread.id), expected_revision=1
                    )
                else:
                    expired = thread.model_copy(
                        update={"attention_expires_at": app.clock.now() - timedelta(seconds=1)}
                    )
                    await save_value(
                        uow.email, app.principal, "thread", str(thread.id), expired, app.clock.now()
                    )
        response = await client.get(path, params=params)
        assert response.status_code == 200
        assert response.json()["snapshot_version"] != before
        assert response.json()["items"] == []


async def test_thread_validator_cannot_bypass_revoked_read_scope() -> None:
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        path = f"/v1/email/threads/{thread.id}"
        first = await client.get(path)
        app.principal.scopes.discard("email.read")
        response = await client.get(path, headers={"If-None-Match": first.headers["etag"]})
        assert response.status_code == 403
        assert response.headers["cache-control"] == "private, no-store"
