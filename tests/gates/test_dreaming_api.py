"""Daily supervised dreaming is an authenticated owner control surface."""

from dataclasses import replace
from typing import cast

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.memory.reconsolidation_apply import apply_review
from tests.contract.reconsolidation_apply_cases import prepared_batch
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_surface_cases import owner
from tests.contract.support import NOW, session
from tests.gates.test_memory_api_boundary_m17 import _client
from tests.integration.m2_support import memory_settings


async def test_review_dashboard_approval_changes_real_store_and_replays() -> None:
    settings = replace(
        memory_settings(),
        memory_api_enabled=True,
        memory_reconsolidation_api_enabled=True,
        memory_dreaming_review_enabled=True,
    )
    async with build(
        settings=settings, storage="memory", principal=owner(), clock=FixedClock(NOW)
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        prepared, review = await prepared_batch(cast(Factory, app.uow_factory))
        result = await apply_review(
            app.uow_factory,
            FixedClock(NOW),
            owner(),
            prepared,
            review,
            admitted=lambda: True,
            owner_review=True,
        )
        key = result[0].operation_ids[0]
        async with _client(app) as client:
            status = await client.get("/v1/dreaming")
            assert status.status_code == 200, "daily dreaming needs an authenticated status API"
            assert status.json()["schedule"]["paused"] is False
            page = await client.get("/dreaming")
            assert page.status_code == 200 and "Approve and apply" in page.text
            before = (
                await client.get(f"/v1/memory-reconsolidations/{key}?ceiling=restricted")
            ).json()
            path = f"/v1/dreaming/{key}/decision?ceiling=restricted"
            for _ in range(2):
                after = await client.post(
                    path,
                    json={"decision": "approved", "expected_revision": before["revision"]},
                    headers={"Idempotency-Key": "approve"},
                )
                assert after.status_code == 200, after.text
                assert after.json()["state"] == "committed"
                assert after.headers["cache-control"] == "private, no-store"
            undone = await client.post(
                f"/v1/memory-reconsolidations/{key}/undo?ceiling=restricted",
                json={"expected_revision": after.json()["revision"]},
                headers={"Idempotency-Key": "undo"},
            )
            assert undone.status_code == 200 and undone.json()["state"] == "undone"


async def test_review_api_auth_validation_and_disabled_flag() -> None:
    from uuid import UUID

    settings = replace(
        memory_settings(),
        memory_api_enabled=True,
        memory_reconsolidation_api_enabled=True,
        memory_dreaming_review_enabled=True,
    )
    async with build(
        settings=settings, storage="memory", principal=owner(), clock=FixedClock(NOW)
    ) as app:
        async with _client(app) as client:
            denied = await client.get("/v1/dreaming", headers={"Forwarded": "for=external"})
            assert denied.status_code == 401
            for body in (
                {"expected_revision": True, "paused": True},
                {"expected_revision": 1, "paused": "true"},
                {"expected_revision": 1, "paused": True, "extra": 1},
            ):
                assert (await client.post("/v1/dreaming/schedule", json=body)).status_code == 400
            for body in (
                {"expected_revision": 1, "decision": "approved", "text": "changed"},
                {"expected_revision": True, "decision": "approved"},
                {"expected_revision": 1, "decision": "maybe"},
            ):
                r = await client.post(
                    f"/v1/dreaming/{UUID(int=1)}/decision?ceiling=restricted",
                    json=body,
                    headers={"Idempotency-Key": "x"},
                )
                assert r.status_code == 400
            paused = await client.post(
                "/v1/dreaming/schedule", json={"expected_revision": 1, "paused": True}
            )
            assert paused.status_code == 200 and paused.json()["paused"]
        async with _client(app, principal=owner().model_copy(update={"scopes": set()})) as client:
            assert (await client.get("/v1/dreaming")).status_code == 403
            assert (
                await client.post(
                    "/v1/dreaming/schedule", json={"expected_revision": 1, "paused": True}
                )
            ).status_code == 403
    async with (
        build(settings=memory_settings(), storage="memory", principal=owner()) as app,
        _client(app) as client,
    ):
        assert (await client.get("/v1/dreaming")).status_code == 404
        assert (await client.get("/dreaming")).status_code == 404


async def test_dreaming_session_survives_reconnect_and_is_narrowly_scoped() -> None:
    from httpx import ASGITransport, AsyncClient
    from pydantic import SecretStr

    from agent_core.api.app import create_app
    from agent_core.config import AuthMode

    settings = replace(
        memory_settings(),
        auth_mode=AuthMode.TOKEN,
        auth_token=SecretStr("test-dreaming-owner-secret"),
        memory_api_enabled=True,
        memory_reconsolidation_api_enabled=True,
        memory_dreaming_review_enabled=True,
    )
    async with build(
        settings=replace(settings, auth_mode=AuthMode.DEV), storage="memory", principal=owner()
    ) as app:
        api_app = create_app(
            app.services,
            settings,
            owner(),
            app.new_request_id,
            app.readiness_probe,
            clock=app.clock,
        )
        async with AsyncClient(
            transport=ASGITransport(app=api_app), base_url="https://testserver"
        ) as client:
            headers = {"Origin": "https://testserver", "X-Dreaming-Session": "1"}
            login = await client.post(
                "/v1/dreaming/session",
                headers={**headers, "Authorization": "Bearer test-dreaming-owner-secret"},
            )
            assert login.status_code == 200, "connecting must issue a persistent review session"
            cookie = login.headers["set-cookie"]
            assert all(
                flag in cookie
                for flag in ("HttpOnly", "Secure", "SameSite=strict", "Max-Age=2592000")
            )
            assert "test-dreaming-owner-secret" not in cookie
            assert (await client.get("/v1/dreaming", headers=headers)).status_code == 200
            assert (
                await client.get("/v1/memory-reconsolidations?ceiling=restricted", headers=headers)
            ).status_code == 200
            assert (await client.get("/v1/sessions", headers=headers)).status_code == 401
            denied = await client.get("/v1/dreaming")
            assert denied.status_code == 401
            assert denied.headers.get("cache-control") == "private, no-store"
            assert (
                await client.post(
                    "/v1/dreaming/schedule",
                    headers={**headers, "Origin": "https://attacker.invalid"},
                    json={"paused": True, "expected_revision": 1},
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/v1/dreaming/schedule",
                    headers=headers,
                    json={"paused": True, "expected_revision": 1},
                )
            ).status_code == 200
            assert (await client.delete("/v1/dreaming/session", headers=headers)).status_code == 200
            assert (await client.get("/v1/dreaming", headers=headers)).status_code == 401


async def test_status_explains_legacy_erasure_block_without_exposing_records() -> None:
    from uuid import UUID

    from agent_core.adapters.persistence.people import InMemoryPeopleStore

    settings = replace(
        memory_settings(),
        memory_api_enabled=True,
        memory_reconsolidation_api_enabled=True,
        memory_dreaming_review_enabled=True,
    )
    async with build(settings=settings, storage="memory", principal=owner()) as app:
        async with app.uow_factory() as uow:
            people = cast(InMemoryPeopleStore, uow.people)
            people._erased.add((owner().tenant_id, owner().principal_id, UUID(int=9876)))
        async with _client(app) as client:
            result = await client.get("/v1/dreaming")
            assert result.json().get("blockers") == ["legacy_attribution_unavailable"]
            assert str(UUID(int=9876)) not in result.text
