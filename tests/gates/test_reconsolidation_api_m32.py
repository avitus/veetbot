"""M32 owner API boundaries; full native/derived-memory gates remain separate."""

from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.api import create_app
from agent_core.bootstrap import build
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_operation_cases import committed
from tests.contract.reconsolidation_surface_cases import owner
from tests.contract.support import NOW, session
from tests.gates.test_memory_api_boundary_m17 import _client
from tests.integration.m2_support import memory_settings


async def test_operation_routes_inspect_and_undo_with_processing_disabled() -> None:
    settings = replace(
        memory_settings(), memory_api_enabled=True, memory_reconsolidation_api_enabled=True
    )
    async with build(
        settings=settings, storage="memory", principal=owner(), clock=FixedClock(NOW)
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        _, _, operation = await committed(cast(Factory, app.uow_factory))
        path = f"/v1/memory-reconsolidations/{operation.id}"
        async with _client(app) as client:
            result = await client.get(path, params={"ceiling": "internal"})
            assert result.status_code == 200, result.text
            assert result.json()["content"]["memory_id"] == str(operation.plan.canonical_id)
            assert result.headers["cache-control"] == "private, no-store"
            assert set(result.json()) == {
                "id",
                "kind",
                "state",
                "revision",
                "reason",
                "policy",
                "model_identity",
                "created_at",
                "committed_at",
                "invalidated_at",
                "undone_at",
                "content",
                "sources",
            }
            listing = await client.get(
                "/v1/memory-reconsolidations", params={"ceiling": "internal", "limit": 999}
            )
            assert listing.status_code == 200 and len(listing.json()["items"]) == 1
            for _ in range(2):
                undo = await client.post(
                    path + "/undo",
                    params={"ceiling": "internal"},
                    json={"expected_revision": 1},
                    headers={"Idempotency-Key": "undo"},
                )
                assert undo.status_code == 200, undo.text
                assert undo.json()["state"] == "undone" and undo.json()["revision"] == 2
                assert undo.headers["cache-control"] == "private, no-store"
            conflict = await client.post(
                path + "/undo",
                params={"ceiling": "internal"},
                json={"expected_revision": 2},
                headers={"Idempotency-Key": "undo"},
            )
            assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "conflict"
            async with app.uow_factory() as uow:
                await uow.memories.fence_for_erasure(owner(), [operation.plan.member_ids[-1]])
            replay = await client.post(
                path + "/undo",
                params={"ceiling": "restricted"},
                json={"expected_revision": 1},
                headers={"Idempotency-Key": "undo"},
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["content"] is None and replay.json()["sources"] == []
            assert (await client.get(path, params={"ceiling": "internal"})).status_code == 404
        for foreign in (
            owner().model_copy(update={"principal_id": "foreign"}),
            owner().model_copy(update={"tenant_id": "foreign"}),
        ):
            async with _client(app, principal=foreign) as client:
                assert (await client.get(path, params={"ceiling": "restricted"})).status_code == 404
                listing = await client.get(
                    "/v1/memory-reconsolidations", params={"ceiling": "restricted"}
                )
                assert listing.status_code == 200 and listing.json()["items"] == []
                result = await client.post(
                    path + "/undo",
                    params={"ceiling": "restricted"},
                    json={"expected_revision": 1},
                    headers={"Idempotency-Key": "undo"},
                )
                assert result.status_code == 404 and result.json()["error"]["code"] == "not_found"


async def test_operation_route_validation_and_authorization() -> None:
    settings = replace(
        memory_settings(), memory_api_enabled=True, memory_reconsolidation_api_enabled=True
    )
    async with build(
        settings=settings, storage="memory", principal=owner(), clock=FixedClock(NOW)
    ) as app:
        async with _client(app) as client:
            for method, target in (
                ("GET", "/v1/memory-reconsolidations"),
                ("GET", f"/v1/memory-reconsolidations/{UUID(int=123)}"),
                ("POST", f"/v1/memory-reconsolidations/{UUID(int=123)}/undo"),
            ):
                denied = await client.request(method, target, headers={"Forwarded": "for=example"})
                assert denied.status_code == 401
                assert denied.json()["error"]["code"] in {
                    "authentication_failed",
                    "authentication_error",
                }
            legacy = await client.get("/v1/memories")
            assert (
                legacy.status_code == 400 and legacy.json()["error"]["code"] == "malformed_request"
            )
            for params in (
                {},
                {"ceiling": "wrong"},
                {"ceiling": "internal", "kind": "bad"},
                {"ceiling": "internal", "limit": 0},
                {"ceiling": "internal", "cursor": "garbage"},
            ):
                response = await client.get("/v1/memory-reconsolidations", params=params)
                assert response.status_code == 400, response.text
                assert response.json()["error"]["code"] == "validation_error"
            path = f"/v1/memory-reconsolidations/{UUID(int=123)}"
            for body, headers in (
                ({}, {"Idempotency-Key": "x"}),
                ({"expected_revision": 0}, {"Idempotency-Key": "x"}),
                ({"expected_revision": True}, {"Idempotency-Key": "x"}),
                ({"expected_revision": 1, "text": "forbidden"}, {"Idempotency-Key": "x"}),
                ({"expected_revision": 1}, {}),
                ({"expected_revision": 1}, {"Idempotency-Key": " "}),
            ):
                response = await client.post(
                    path + "/undo", params={"ceiling": "internal"}, json=body, headers=headers
                )
                assert (
                    response.status_code == 400
                    and response.json()["error"]["code"] == "validation_error"
                )
            assert (await client.get(path, params={"ceiling": "internal"})).status_code == 404
        schema = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        ).openapi()
        assert {
            (method, path, operation["required_scope"])
            for path, methods in schema["paths"].items()
            if path.startswith("/v1/memory-reconsolidations")
            for method, operation in methods.items()
        } == {
            ("get", "/v1/memory-reconsolidations", "memory.read"),
            ("get", "/v1/memory-reconsolidations/{operation_id}", "memory.read"),
            ("post", "/v1/memory-reconsolidations/{operation_id}/undo", "memory.write"),
        }
        async with _client(app, principal=owner().model_copy(update={"scopes": set()})) as client:
            assert (
                await client.get("/v1/memory-reconsolidations", params={"ceiling": "restricted"})
            ).status_code == 403
            assert (
                await client.post(
                    path + "/undo",
                    params={"ceiling": "restricted"},
                    json={"expected_revision": 1},
                    headers={"Idempotency-Key": "x"},
                )
            ).status_code == 403


@pytest.mark.parametrize(
    "memory_enabled,recon_enabled", [(False, False), (True, False), (False, True)]
)
async def test_operation_routes_require_both_surface_flags(
    memory_enabled: bool, recon_enabled: bool
) -> None:
    settings = replace(
        memory_settings(),
        memory_api_enabled=memory_enabled,
        memory_reconsolidation_api_enabled=recon_enabled,
    )
    async with (
        build(settings=settings, storage="memory", principal=owner()) as app,
        _client(app) as client,
    ):
        assert (
            await client.get("/v1/memory-reconsolidations", params={"ceiling": "restricted"})
        ).status_code == 404
        schema = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        ).openapi()
        assert not any(p.startswith("/v1/memory-reconsolidations") for p in schema["paths"])
