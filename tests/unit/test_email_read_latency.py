"""Reading one conversation must not wait for mailbox-wide housekeeping."""

import asyncio
from datetime import timedelta

import pytest

from agent_core.adapters.persistence.email import InMemoryEmailStore
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailDraftStatus, EmailRecord
from tests.gates.test_email_experience_m26 import email_client, seed_mail


@pytest.mark.parametrize("resource", ["thread", "draft", "draft_revisions"])
async def test_detail_reads_do_not_scan_unrelated_mail(
    monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    """Even expired mail elsewhere cannot turn a point read into global cleanup."""
    async with email_client() as (app, client):
        thread, draft = await seed_mail(app)
        other, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "thread", str(other.id))
            assert row is not None
            old = other.model_copy(
                update={"last_accessed_at": app.clock.now() - timedelta(days=31)}
            )
            await uow.email.put(
                row.model_copy(update={"revision": 2, "payload": old.model_dump(mode="json")}),
                expected_revision=1,
            )
        scanned: list[str] = []
        original = InMemoryEmailStore.list

        async def record_scan(
            store: InMemoryEmailStore,
            principal: Principal,
            kind: str,
            *,
            after: str | None = None,
            limit: int = 1000,
        ) -> list[EmailRecord]:
            scanned.append(kind)
            return await original(store, principal, kind, after=after, limit=limit)

        monkeypatch.setattr(InMemoryEmailStore, "list", record_scan)
        path = {
            "thread": f"threads/{thread.id}",
            "draft": f"drafts/{draft.id}",
            "draft_revisions": f"drafts/{draft.id}/revisions",
        }[resource]
        response = await client.get(f"/v1/email/{path}")
        assert response.status_code == 200
        assert "thread" not in scanned and "draft" not in scanned
        if resource == "thread":
            assert response.json()["messages"][0]["body"] == thread.messages[0].body
            assert response.json()["draft"]["body"] == draft.body
        async with app.uow_factory() as uow:
            unchanged = await uow.email.get(app.principal, "thread", str(other.id))
        assert unchanged is not None and unchanged.revision == 2


async def test_point_reads_still_enforce_body_and_draft_retention() -> None:
    """Removing global cleanup must never return or renew expired bodies."""
    async with email_client() as (app, client):
        thread, draft = await seed_mail(app)
        old = app.clock.now() - timedelta(days=31)
        async with app.uow_factory() as uow:
            for kind, value in (
                ("thread", thread.model_copy(update={"last_accessed_at": old})),
                (
                    "draft",
                    draft.model_copy(update={"status": EmailDraftStatus.SENT, "updated_at": old}),
                ),
            ):
                row = await uow.email.get(app.principal, kind, str(value.id))
                assert row is not None
                await uow.email.put(
                    row.model_copy(
                        update={"revision": 2, "payload": value.model_dump(mode="json")}
                    ),
                    expected_revision=1,
                )
        response = await client.get(f"/v1/email/threads/{thread.id}")
        assert response.status_code == 200
        assert response.json()["messages"][0]["body"] == ""
        assert response.json()["complete"] is False
        assert response.json()["draft"]["body"] == ""
        assert (await client.get(f"/v1/email/drafts/{draft.id}")).json()["body"] == ""
        assert (await client.get(f"/v1/email/drafts/{draft.id}/revisions")).json()["items"] == []


@pytest.mark.parametrize("background", ["inbox", "maintenance"])
async def test_mailbox_scan_does_not_hold_up_selected_thread(
    monkeypatch: pytest.MonkeyPatch, background: str
) -> None:
    """A deliberately held scan must not retain the owner's email mutation lock."""
    async with email_client() as (app, _):
        thread, _ = await seed_mail(app)
        scanning, release = asyncio.Event(), asyncio.Event()
        original = InMemoryEmailStore.list

        async def delayed_scan(
            store: InMemoryEmailStore,
            principal: Principal,
            kind: str,
            *,
            after: str | None = None,
            limit: int = 1000,
        ) -> list[EmailRecord]:
            if kind == "thread" and not scanning.is_set():
                scanning.set()
                await release.wait()
            return await original(store, principal, kind, after=after, limit=limit)

        monkeypatch.setattr(InMemoryEmailStore, "list", delayed_scan)
        service = app.services.email
        work = asyncio.create_task(
            service.threads(app.principal)
            if background == "inbox"
            else service.expire_cache(app.principal)
        )
        try:
            await asyncio.wait_for(scanning.wait(), timeout=1)
            result = await asyncio.wait_for(service.thread(app.principal, thread.id), timeout=1)
            assert result["id"] == str(thread.id)
        finally:
            release.set()
            await work


async def test_maintenance_rechecks_a_refreshed_candidate_before_erasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source refreshed after discovery must survive a stale cleanup candidate."""
    async with email_client() as (app, _):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "thread", str(thread.id))
            assert row is not None
            old = thread.model_copy(
                update={"last_accessed_at": app.clock.now() - timedelta(days=31)}
            )
            await uow.email.put(
                row.model_copy(update={"revision": 2, "payload": old.model_dump(mode="json")}),
                expected_revision=1,
            )
        original = InMemoryEmailStore.list

        async def refresh_after_snapshot(
            store: InMemoryEmailStore,
            principal: Principal,
            kind: str,
            *,
            after: str | None = None,
            limit: int = 1000,
        ) -> list[EmailRecord]:
            page = await original(store, principal, kind, after=after, limit=limit)
            if kind == "thread" and page:
                refreshed = thread.model_copy(update={"last_accessed_at": app.clock.now()})
                await store.put(
                    page[0].model_copy(
                        update={"revision": 3, "payload": refreshed.model_dump(mode="json")}
                    ),
                    expected_revision=2,
                )
            return page

        monkeypatch.setattr(InMemoryEmailStore, "list", refresh_after_snapshot)
        assert await app.services.email.expire_cache(app.principal) == 0
        result = await app.services.email.thread(app.principal, thread.id)
        assert result["messages"] == [
            message.model_dump(mode="json") for message in thread.messages
        ]
