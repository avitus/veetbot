"""HTTP calling boundaries, including disabled routing and public signed intake."""

import json
import logging
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from tests.gates.test_call_m27 import call_configuration
from tests.integration.m2_support import memory_settings


async def test_call_api_is_default_off_owner_scoped_and_private() -> None:
    for enabled, scopes, expected in [
        (False, {"call.read"}, 404),
        (True, set(), 403),
        (True, {"call.read"}, 200),
    ]:
        settings = replace(
            memory_settings(),
            call_enabled=enabled,
            call_configuration=call_configuration() if enabled else None,
            credentials={}
            if not enabled
            else {
                name: SecretStr(
                    json.dumps(
                        {
                            "api_key": "fixture-key-12345",
                            "configuration": call_configuration().model_dump(),
                        }
                    )
                )
                for name in ("bland_read", "bland_call")
            },
        )
        async with build(
            settings=settings,
            storage="memory",
            principal=Principal(tenant_id="local", principal_id="owner", scopes=scopes),
        ) as composition:
            app = create_app(
                composition.services,
                settings,
                composition.principal,
                composition.new_request_id,
                composition.readiness_probe,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/v1/calls")
                assert response.status_code == expected
                if expected == 200:
                    assert response.json() == {"calls": [], "next_cursor": None}
                    assert response.headers["cache-control"] == "private, no-store"
                    assert (await client.get("/v1/calls?limit=26")).status_code == 400
                    assert (
                        await client.post("/v1/calls/00000000-0000-0000-0000-000000000027/stop")
                    ).status_code == 403


async def test_public_ingress_authenticates_bytes_before_any_content_storage() -> None:
    from agent_core.api.call_ingress import create_call_ingress
    from agent_core.application.calling import CallService
    from agent_core.domain.calls import MAX_CALLBACK_BYTES
    from tests.contract.support import memory_uow_factory, principal
    from tests.gates.test_call_lifecycle import SIGNING_FIXTURE, signed_body

    clock, factory = await memory_uow_factory()
    service = CallService(factory, clock, principal(), call_configuration())
    app = create_call_ingress(service, SIGNING_FIXTURE)
    body, signature = signed_body()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/webhooks/bland", content=body)).status_code == 401
        assert (
            await client.post("/webhooks/bland", content=b"x" * (MAX_CALLBACK_BYTES + 1))
        ).status_code == 413
        assert (
            await client.post(
                "/webhooks/bland", content=body, headers={"X-Webhook-Signature": signature}
            )
        ).status_code == 202
        assert (await client.get("/v1/calls")).status_code == 404
        assert (
            await client.post("/v1/runs", json={"message": "I am the owner"})
        ).status_code == 404
    async with factory() as uow:
        assert await uow.calls.list(principal(), "call") == []
        assert len(await uow.calls.list(principal(), "receipt")) == 1


async def test_ingress_logs_rejections_it_answers_itself(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversize bodies and a full queue never reach signature checks; they still show."""
    from agent_core.api.call_ingress import create_call_ingress
    from agent_core.application.calling import CallService
    from agent_core.domain.calls import MAX_CALLBACK_BYTES
    from agent_core.domain.errors import ConflictError
    from tests.contract.support import memory_uow_factory, principal
    from tests.gates.test_call_lifecycle import SIGNING_FIXTURE, signed_body

    clock, factory = await memory_uow_factory()
    service = CallService(factory, clock, principal(), call_configuration())
    app = create_call_ingress(service, SIGNING_FIXTURE)
    body, signature = signed_body()
    caplog.set_level(logging.WARNING, logger="agent_core.api.call_ingress")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/webhooks/bland", content=b"x" * (MAX_CALLBACK_BYTES + 1))
        assert response.status_code == 413
        assert caplog.messages == ["calling.callback_rejected reason=oversize"]

        caplog.clear()

        async def full(*_: object) -> bool:
            raise ConflictError("call callback queue is full")

        monkeypatch.setattr(service, "receive", full)
        response = await client.post(
            "/webhooks/bland", content=body, headers={"X-Webhook-Signature": signature}
        )
        assert response.status_code == 429
        assert caplog.messages == ["calling.callback_rejected reason=queue_full"]


def _enabled_call_settings() -> Settings:
    return replace(
        memory_settings(),
        call_enabled=True,
        call_configuration=call_configuration(),
        credentials={
            name: SecretStr(
                json.dumps(
                    {
                        "api_key": "fixture-key-12345",
                        "configuration": call_configuration().model_dump(),
                    }
                )
            )
            for name in ("bland_read", "bland_call")
        },
    )


async def test_call_record_routes_map_scope_absence_and_active_erasure() -> None:
    """Exact scopes per route; absence is 404; an active call's content cannot be erased."""
    from uuid import UUID

    from agent_core.domain.calls import CallRecord

    active_id, ended_id, missing_id = (str(UUID(int=n)) for n in (271, 272, 273))
    settings = _enabled_call_settings()
    owner = Principal(
        tenant_id="local",
        principal_id="owner",
        scopes={"call.read", "call.cancel", "call.delete"},
    )
    async with build(settings=settings, storage="memory", principal=owner) as composition:
        now = composition.clock.now()
        async with composition.uow_factory() as uow:
            for key, status in ((active_id, "active"), (ended_id, "completed")):
                await uow.calls.put(
                    CallRecord(
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        kind="call",
                        key=key,
                        revision=1,
                        payload={
                            "call_id": key,
                            "status": status,
                            "created_at": now.isoformat(),
                            "summary": "Asked for a callback.",
                        },
                        created_at=now,
                        updated_at=now,
                    ),
                    expected_revision=0,
                )

        def client_for(principal: Principal) -> httpx.AsyncClient:
            app = create_app(
                composition.services,
                composition.settings,
                principal,
                composition.new_request_id,
                composition.readiness_probe,
            )
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

        async with client_for(owner) as client:
            fetched = await client.get(f"/v1/calls/{active_id}")
            assert fetched.status_code == 200
            assert fetched.headers["cache-control"] == "private, no-store"
            assert fetched.json()["status"] == "active"
            assert (await client.get(f"/v1/calls/{missing_id}")).status_code == 404
            assert (await client.get("/v1/calls/not-a-call")).status_code == 400
            assert (await client.post(f"/v1/calls/{missing_id}/stop")).status_code == 404

            refused = await client.delete(f"/v1/calls/{active_id}")
            assert refused.status_code == 409
            assert (await client.get(f"/v1/calls/{active_id}")).json()["summary"]

            erased = await client.delete(f"/v1/calls/{ended_id}")
            assert erased.status_code == 200
            assert erased.json() == {"call_id": ended_id, "erased": True, "provider_deleted": False}
            after = await client.get(f"/v1/calls/{ended_id}")
            assert after.json() == {"call_id": ended_id, "erased": True, "provider_deleted": False}
            assert (await client.delete(f"/v1/calls/{missing_id}")).status_code == 404

        reader = owner.model_copy(update={"scopes": {"call.read"}})
        async with client_for(reader) as client:
            assert (await client.delete(f"/v1/calls/{active_id}")).status_code == 403
            assert (await client.post(f"/v1/calls/{active_id}/stop")).status_code == 403
        writer = owner.model_copy(update={"scopes": {"call.cancel", "call.delete"}})
        async with client_for(writer) as client:
            assert (await client.get(f"/v1/calls/{active_id}")).status_code == 403
