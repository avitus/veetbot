"""A2 mailbox collections count authorized cached mail before pagination."""

from uuid import uuid4

import pytest

from agent_core.domain.email import EmailRecord
from tests.gates.test_email_experience_m26 import email_client, seed_mail


async def test_collections_count_before_paging_and_keep_handled_distinct_from_archive() -> None:
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
            assert data["collection_counts"] == {"inbox": 7, "needs_you": 6, "handled": 1}
            assert len(data["items"]) == 1
            assert (data["next_cursor"] is not None) == (count > 1)
            if view == "handled":
                assert data["items"][0]["id"] == str(first.id)
                assert data["items"][0]["in_inbox"] is True
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
            ({"view": "inbox"}, {"inbox": 3, "needs_you": 2, "handled": 0}),
            ({"view": "inbox", "account_id": "hidden"}, {"inbox": 0, "needs_you": 0, "handled": 0}),
            ({"view": "handled", "text": "no match"}, {"inbox": 0, "needs_you": 0, "handled": 0}),
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
