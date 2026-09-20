"""HTTP calling boundaries, including disabled routing and public signed intake."""

import json
import logging
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from agent_core.api import create_app
from agent_core.bootstrap import build
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
