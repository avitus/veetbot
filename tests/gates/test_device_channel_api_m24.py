"""Milestone 24 device-channel HTTP gates: fetch, result, and ingest.

The three routes are the phone's whole server surface. Each one revalidates the
device's presence before it reads anything, because the invocation store
resolves a row by identifier alone; ownership is this layer's job, not the
store's.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi.routing import APIRoute

from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.devices import (
    DeviceCapability,
    DeviceInvocation,
    DeviceInvocationStatus,
    DeviceStatus,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from tests.contract.support import SHIPPED_INVOCATION_TIMEOUT_SECONDS
from tests.contract.test_device_registry_contract import device
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002c0")
FOREIGN_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002c1")
REVOKED_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002c2")
INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002c3")
EXPIRED_INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002c4")
RUN_ID = UUID("00000000-0000-0000-0000-0000000002c5")
SENDER = "+15555550123"
BODY = "Marzipan needs feeding at six."
TOOL_NAME = DeviceCapability.SMS_SEND.value


def _settings() -> Settings:
    return replace(
        memory_settings(),
        device_channel_enabled=True,
        device_sms_enabled=True,
    )


@asynccontextmanager
async def _client(composition: Composition, *, principal: Principal | None = None) -> Any:
    app = create_app(
        composition.services,
        composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


@asynccontextmanager
async def _composition(*, turns: int = 2) -> Any:
    async with build(
        settings=_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Triaged.") for _ in range(turns)]),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        yield composition


async def _seed_devices(composition: Composition) -> None:
    principal = composition.principal
    owned = device(device_id=DEVICE_ID, capabilities=frozenset({TOOL_NAME})).model_copy(
        update={"tenant_id": principal.tenant_id, "principal_id": principal.principal_id}
    )
    revoked = device(
        device_id=REVOKED_DEVICE_ID,
        client_device_id="revoked-installation",
        token=None,
        capabilities=frozenset({TOOL_NAME}),
    ).model_copy(
        update={
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
            "status": DeviceStatus.REVOKED,
            "revoked_at": NOW,
        }
    )
    foreign = device(
        device_id=FOREIGN_DEVICE_ID,
        client_device_id="foreign-installation",
        principal_id="someone-else",
        token="push-token-b",  # noqa: S106
        capabilities=frozenset({TOOL_NAME}),
    ).model_copy(update={"tenant_id": principal.tenant_id, "principal_id": "someone-else"})
    async with composition.uow_factory() as uow:
        await uow.devices.upsert(owned, principal)
        await uow.devices.upsert(revoked, principal)
        await uow.devices.upsert(
            foreign,
            principal.model_copy(update={"principal_id": "someone-else"}, deep=True),
        )


def _pending(
    invocation_id: UUID,
    *,
    tenant_id: str,
    device_id: UUID = DEVICE_ID,
    status: DeviceInvocationStatus = DeviceInvocationStatus.PENDING,
) -> DeviceInvocation:
    return DeviceInvocation(
        id=invocation_id,
        tenant_id=tenant_id,
        device_id=device_id,
        run_id=RUN_ID,
        tool_name=TOOL_NAME,
        arguments={"recipient": SENDER, "body": "Feeding at six."},
        status=status,
        created_at=NOW,
        resolved_at=None if status is DeviceInvocationStatus.PENDING else NOW,
    )


def _message(body: str = BODY, **updates: object) -> dict[str, object]:
    return {
        "channel": "sms",
        "sender": SENDER,
        "body": body,
        "received_at": NOW.isoformat(),
        **updates,
    }


def _device_channel_routes(app: Any) -> list[APIRoute]:
    """Flatten mounted routers so a disabled flag is visible as an absent route."""

    flattened = [
        nested
        for route in app.routes
        for nested in (
            route.original_router.routes if hasattr(route, "original_router") else (route,)
        )
    ]
    return [
        route
        for route in flattened
        if isinstance(route, APIRoute)
        and route.path.startswith("/v1/devices/")
        and ("/invocations" in route.path or route.path.endswith("/messages"))
    ]


async def test_the_device_channel_routes_declare_exactly_three_scoped_paths() -> None:
    async with _composition() as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
    routes = _device_channel_routes(app)

    assert {
        (route.path, method, (route.openapi_extra or {})["required_scope"])
        for route in routes
        for method in (route.methods or set())
    } == {
        ("/v1/devices/{device_id}/invocations", "GET", "device.read"),
        (
            "/v1/devices/{device_id}/invocations/{invocation_id}/result",
            "POST",
            "device.write",
        ),
        ("/v1/devices/{device_id}/messages", "POST", "device.write"),
    }


async def test_the_device_channel_router_is_absent_while_the_flag_is_off() -> None:
    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        async with _client(composition) as client:
            fetched = await client.get(f"/v1/devices/{DEVICE_ID}/invocations")
            ingested = await client.post(f"/v1/devices/{DEVICE_ID}/messages", json=_message())

    assert _device_channel_routes(app) == []
    assert fetched.status_code == 404
    assert ingested.status_code == 404


async def test_a_device_fetches_its_pending_invocations_oldest_first() -> None:
    async with _composition() as composition:
        await _seed_devices(composition)
        async with composition.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(INVOCATION_ID, tenant_id=composition.principal.tenant_id)
            )
            await uow.device_invocations.create(
                _pending(
                    EXPIRED_INVOCATION_ID,
                    tenant_id=composition.principal.tenant_id,
                    status=DeviceInvocationStatus.SENT,
                )
            )
        async with _client(composition) as client:
            fetched = await client.get(f"/v1/devices/{DEVICE_ID}/invocations")

    assert fetched.status_code == 200
    assert fetched.json() == {
        "invocations": [
            {
                "id": str(INVOCATION_ID),
                "tool_name": TOOL_NAME,
                "arguments": {"recipient": SENDER, "body": "Feeding at six."},
                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                "expires_at": (NOW + timedelta(seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS))
                .isoformat()
                .replace("+00:00", "Z"),
            }
        ]
    }


async def test_an_unknown_foreign_or_revoked_device_can_neither_fetch_nor_answer() -> None:
    async with _composition() as composition:
        await _seed_devices(composition)
        async with composition.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(INVOCATION_ID, tenant_id=composition.principal.tenant_id)
            )
        async with _client(composition) as client:
            unknown = await client.get(f"/v1/devices/{UUID(int=404)}/invocations")
            foreign = await client.get(f"/v1/devices/{FOREIGN_DEVICE_ID}/invocations")
            revoked = await client.get(f"/v1/devices/{REVOKED_DEVICE_ID}/invocations")
            stolen = await client.post(
                f"/v1/devices/{FOREIGN_DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
            answered_by_revoked = await client.post(
                f"/v1/devices/{REVOKED_DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
        async with composition.uow_factory() as uow:
            untouched = await uow.device_invocations.get(INVOCATION_ID)

    assert unknown.status_code == 404
    assert foreign.status_code == 404
    assert revoked.status_code == 409
    assert revoked.json()["error"]["details"]["reason"] == "device_revoked"
    assert stolen.status_code == 404
    assert answered_by_revoked.status_code == 409
    assert untouched.status is DeviceInvocationStatus.PENDING


async def test_the_first_result_wins_and_an_expired_invocation_refuses_every_post() -> None:
    async with _composition() as composition:
        await _seed_devices(composition)
        async with composition.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(INVOCATION_ID, tenant_id=composition.principal.tenant_id)
            )
            await uow.device_invocations.create(
                _pending(
                    EXPIRED_INVOCATION_ID,
                    tenant_id=composition.principal.tenant_id,
                    status=DeviceInvocationStatus.EXPIRED,
                )
            )
        async with _client(composition) as client:
            sent = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
            replayed = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
            disagreed = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "failed"},
            )
            expired = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{EXPIRED_INVOCATION_ID}/result",
                json={"status": "expired"},
            )
            unknown_status = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "pending"},
            )

    assert sent.status_code == 200
    assert sent.json() == {
        "id": str(INVOCATION_ID),
        "status": "sent",
        "resolved_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    assert replayed.json() == sent.json()
    assert disagreed.json() == sent.json()
    assert expired.status_code == 409
    assert expired.json()["error"]["details"]["reason"] == "device_invocation_expired"
    assert unknown_status.status_code == 400


async def test_the_ingest_route_reports_its_routing_and_never_echoes_the_body() -> None:
    async with _composition(turns=4) as composition:
        await _seed_devices(composition)
        async with _client(composition) as client:
            accepted = await client.post(f"/v1/devices/{DEVICE_ID}/messages", json=_message())
            replayed = await client.post(f"/v1/devices/{DEVICE_ID}/messages", json=_message())
            foreign = await client.post(
                f"/v1/devices/{FOREIGN_DEVICE_ID}/messages", json=_message()
            )
            revoked = await client.post(
                f"/v1/devices/{REVOKED_DEVICE_ID}/messages", json=_message()
            )
            # A device the caller cannot see stays absent even when the body it
            # posted would have been refused on its own merits: presence is
            # revalidated before the message is judged.
            unknown_bad_channel = await client.post(
                f"/v1/devices/{UUID(int=404)}/messages",
                json=_message(channel="imessage"),
            )
            foreign_bad_channel = await client.post(
                f"/v1/devices/{FOREIGN_DEVICE_ID}/messages",
                json=_message(channel="imessage"),
            )
            owned_bad_channel = await client.post(
                f"/v1/devices/{DEVICE_ID}/messages",
                json=_message(channel="imessage"),
            )

    assert accepted.status_code == 202
    assert set(accepted.json()) == {"duplicate", "session_id", "run_id"}
    assert accepted.json()["duplicate"] is False
    assert replayed.status_code == 202
    assert replayed.json() == {**accepted.json(), "duplicate": True}
    assert BODY not in accepted.text
    assert foreign.status_code == 404
    assert revoked.status_code == 409
    assert BODY not in revoked.text
    assert unknown_bad_channel.status_code == 404
    assert foreign_bad_channel.status_code == 404
    assert owned_bad_channel.status_code == 422
    assert owned_bad_channel.json()["error"]["details"] == {"reason": "channel_unsupported"}


async def test_the_daily_cap_answers_429_without_repeating_the_message() -> None:
    async with _composition(turns=4) as composition:
        await _seed_devices(composition)
        composition.services.device_ingest._ingest_daily_cap = 1
        async with _client(composition) as client:
            accepted = await client.post(f"/v1/devices/{DEVICE_ID}/messages", json=_message())
            capped = await client.post(
                f"/v1/devices/{DEVICE_ID}/messages",
                json=_message("The sitter is late."),
            )

    assert accepted.status_code == 202
    assert capped.status_code == 429
    assert capped.json()["error"]["code"] == "device_ingest_error"
    assert capped.json()["error"]["details"] == {"reason": "ingest_daily_cap"}
    assert "sitter" not in capped.text


async def test_a_naive_received_at_is_malformed_rather_than_stored() -> None:
    async with _composition() as composition:
        await _seed_devices(composition)
        async with _client(composition) as client:
            naive = await client.post(
                f"/v1/devices/{DEVICE_ID}/messages",
                json=_message(received_at="2026-09-01T12:00:00"),
            )

    assert naive.status_code == 400
    assert naive.json()["error"]["code"] == "malformed_request"
