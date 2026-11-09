"""A2 mailbox collections count authorized cached mail before pagination."""

from uuid import uuid4

import pytest

from agent_core.domain.email import EmailRecord
from tests.gates.test_email_experience_m26 import email_client, seed_mail


async def test_collections_count_before_paging_and_keep_handled_distinct_from_archive() -> None:
    """Keep collection totals independent of paging, handling and archive state."""
    async with email_client() as (composition, client):
        first, _ = await seed_mail(composition)
        second, _ = await seed_mail(composition, priority=0.1)
        for _ in range(5):
            await seed_mail(composition)
        response = await client.post(
            f"/v1/email/threads/{first.id}/dismiss", json={"expected_revision": 1}
        )
        assert response.status_code == 200
        for view, count in (("inbox", 7), ("needs_you", 6), ("handled", 1)):
            response = await client.get("/v1/email/threads", params={"view": view, "limit": 1})
            assert response.status_code == 200
            data = response.json()
            assert data["collection_counts"] == {
                "priority": 5,
                "inbox": 7,
                "needs_you": 6,
                "handled": 1,
                "suspected_spam": 0,
            }
            assert len(data["items"]) == 1
            assert (data["next_cursor"] is not None) == (count > 1)
            if view == "handled":
                assert data["items"][0]["id"] == str(first.id)
                assert data["items"][0]["in_inbox"] is True
        for params, total in (
            ({"view": "priority", "cursor": "1"}, 5),
            ({"view": "priority", "account_id": "hidden"}, 0),
            ({"view": "priority", "text": "no match"}, 0),
        ):
            response = await client.get("/v1/email/threads", params={**params, "limit": 1})
            assert response.json()["total_count"] == total
        response = await client.get("/v1/email/threads", params={"view": "needs_you"})
        ids = {row["id"] for row in response.json()["items"]}
        # Low-ranked reply requests belong to Needs you, even outside the priority selection.
        response = await client.get("/v1/email/threads", params={"view": "needs_you", "limit": 100})
        assert str(second.id) in {row["id"] for row in response.json()["items"]}
        assert str(first.id) not in ids
        await client.post(
            f"/v1/email/threads/{first.id}/dismiss",
            json={"expected_revision": 1, "dismissed": False},
        )
        response = await client.get("/v1/email/threads", params={"view": "handled"})
        assert response.json()["collection_counts"]["handled"] == 0
        assert response.json()["items"] == []


async def test_collection_counts_obey_scope_account_search_archive_and_source_revision() -> None:
    """Exclude inaccessible mail and apply search, archive, and current-revision handling."""
    async with email_client() as (composition, client):
        base, _ = await seed_mail(composition)
        now = composition.clock.now()
        variations: list[dict[str, object]] = [
            {"in_inbox": False, "dismissed_revision": 1},
            {"revision": 2, "dismissed_revision": 1},
            {"needs_reply": False},
            {"account_id": "hidden"},
        ]
        async with composition.uow_factory() as uow:
            for changes in variations:
                item = base.model_copy(update={**changes, "id": uuid4()})
                await uow.email.put(
                    EmailRecord(
                        tenant_id="local",
                        principal_id="owner",
                        kind="thread",
                        key=str(item.id),
                        revision=1,
                        payload=item.model_dump(mode="json"),
                        created_at=now,
                        updated_at=now,
                    ),
                    expected_revision=0,
                )
        composition.services.email.account_servers["hidden"] = {"read": "hidden_read"}
        for params, expected in (
            (
                {"view": "inbox"},
                {"priority": 3, "inbox": 3, "needs_you": 2, "handled": 0, "suspected_spam": 0},
            ),
            (
                {"view": "inbox", "account_id": "hidden"},
                {"priority": 0, "inbox": 0, "needs_you": 0, "handled": 0, "suspected_spam": 0},
            ),
            (
                {"view": "handled", "text": "no match"},
                {"priority": 0, "inbox": 0, "needs_you": 0, "handled": 0, "suspected_spam": 0},
            ),
        ):
            response = await client.get("/v1/email/threads", params=params)
            assert response.status_code == 200
            assert response.json()["collection_counts"] == expected
        composition.principal.scopes.discard("email.read")
        response = await client.get("/v1/email/threads", params={"view": "inbox"})
        assert response.status_code == 403


@pytest.mark.parametrize("view", ["unknown", "INBOX"])
async def test_collection_rejects_unknown_view(view: str) -> None:
    async with email_client() as (_, client):
        response = await client.get("/v1/email/threads", params={"view": view})
        assert response.status_code == 400


async def test_navigation_counts_and_spam_filter_apply_before_paging() -> None:
    """The canvas rows use scoped totals, and spam never means all low-priority mail."""
    async with email_client() as (composition, client):
        base, _ = await seed_mail(composition)
        await seed_mail(composition, priority=0.1)
        now = composition.clock.now()
        async with composition.uow_factory() as uow:
            for changes in (
                {"suspected_spam": True},
                {"suspected_spam": False, "spam_cleared": True},
                {"suspected_spam": True, "in_inbox": False, "in_spam": True},
                {"suspected_spam": True, "account_id": "hidden"},
            ):
                item = base.model_copy(update={**changes, "id": uuid4()})
                await uow.email.put(
                    EmailRecord(
                        tenant_id="local",
                        principal_id="owner",
                        kind="thread",
                        key=str(item.id),
                        revision=1,
                        payload=item.model_dump(mode="json"),
                        created_at=now,
                        updated_at=now,
                    ),
                    expected_revision=0,
                )
        composition.services.email.account_servers["hidden"] = {"read": "hidden_read"}
        for params, count in (
            ({"view": "suspected_spam"}, 1),
            ({"view": "suspected_spam", "account_id": "hidden"}, 0),
            ({"view": "suspected_spam", "text": "no match"}, 0),
            ({"view": "suspected_spam", "cursor": "1"}, 1),
        ):
            response = await client.get("/v1/email/threads", params={**params, "limit": 1})
            assert response.status_code == 200
            data = response.json()
            assert data["total_count"] == count
            assert data["collection_counts"]["suspected_spam"] == count
            assert all(row["suspected_spam"] and not row["in_spam"] for row in data["items"])
            if "cursor" in params:
                assert data["items"] == []
        response = await client.get("/v1/email/threads", params={"view": "inbox", "limit": 1})
        assert response.json()["collection_counts"]["priority"] == 2
        composition.principal.scopes.discard("email.read")
        response = await client.get("/v1/email/threads", params={"view": "suspected_spam"})
        assert response.status_code == 403
