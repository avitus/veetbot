"""Explicit owner endorsement, separate from automatic generated-draft learning."""

from uuid import uuid4

import pytest

from agent_core.application.email import save_value
from agent_core.domain.email import EmailLearningState
from tests.gates.test_email_experience_m26 import email_client, seed_mail


async def test_explicit_style_example_is_revision_bound_idempotent_and_preserves_pause() -> None:
    async with email_client() as (app, client):
        _, draft = await seed_mail(app)
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await save_value(
                uow.email,
                app.principal,
                "learning",
                "shared",
                EmailLearningState(paused=True),
                app.clock.now(),
            )
            await save_value(
                uow.email,
                app.principal,
                "draft",
                str(draft.id),
                draft.model_copy(update={"body": "An explicitly endorsed phrase. " * 100}),
                app.clock.now(),
            )
        endpoint = f"/v1/email/drafts/{draft.id}/style-example"
        first = await client.post(endpoint, json={"expected_revision": 1})
        assert first.status_code == 200
        assert first.headers["cache-control"] == "private, no-store"
        assert first.json()["paused"] is True
        assert first.json()["profile_revision"] == 2
        assert first.json()["style_examples"] == 1
        replay = await client.post(endpoint, json={"expected_revision": 1})
        assert replay.status_code == 200
        assert replay.json() == first.json()
        async with app.uow_factory() as uow:
            [example] = await uow.email.list(app.principal, "style")
        assert example.payload["authorship"] == "owner_endorsed"
        assert example.payload["independent"] is False
        assert example.payload["draft_id"] == str(draft.id)
        assert example.payload["draft_revision"] == 1
        assert len(str(example.payload["excerpt"])) == 2000


@pytest.mark.parametrize("missing_scope", ["email.write", "mcp.gmail_read.use"])
async def test_style_example_requires_current_write_and_account_authority(
    missing_scope: str,
) -> None:
    async with email_client() as (app, client):
        _, draft = await seed_mail(app)
        app.principal.scopes.remove(missing_scope)
        result = await client.post(
            f"/v1/email/drafts/{draft.id}/style-example", json={"expected_revision": 1}
        )
        assert result.status_code == 403
        async with app.uow_factory() as uow:
            assert not await uow.email.list(app.principal, "style")


async def test_style_example_rejects_missing_draft_invalid_revision_and_conflict() -> None:
    async with email_client() as (app, client):
        _, draft = await seed_mail(app)
        for identity, revision, status in (
            (uuid4(), 1, 404),
            (draft.id, 0, 400),
            (draft.id, 2, 409),
        ):
            result = await client.post(
                f"/v1/email/drafts/{identity}/style-example", json={"expected_revision": revision}
            )
            assert result.status_code == status
        async with app.uow_factory() as uow:
            assert not await uow.email.list(app.principal, "style")


async def test_explicit_style_example_succeeds_with_write_scope_and_no_read_scope() -> None:
    async with email_client() as (app, client):
        _, draft = await seed_mail(app)
        app.principal.scopes.remove("email.read")
        response = await client.post(
            f"/v1/email/drafts/{draft.id}/style-example", json={"expected_revision": 1}
        )
        assert response.status_code == 200
        assert response.json()["style_examples"] == 1
