"""Device lifecycle and offline notification API gates."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import SecretStr

from agent_core.adapters.push import FakePushTransport
from agent_core.api import create_app
from agent_core.api.app import DeviceRegistrationRequest
from agent_core.application.notification_dispatcher import NotificationDispatcher
from agent_core.bootstrap import Composition, build
from agent_core.config import (
    AuthMode,
    ConfigurationError,
    DeploymentMode,
    SandboxMechanism,
)
from agent_core.domain.agents import Principal
from agent_core.domain.devices import Device, DeviceKind, PushProvider
from agent_core.domain.notifications import (
    NOTIFICATION_TITLES,
    DeliveryOutcome,
    NewNotification,
    NotificationDelivery,
    NotificationKind,
    NotificationPayload,
    NotificationSeverity,
    PushOutcome,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import OccurrenceDisposition
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import NOW
from tests.contract.test_device_registry_contract import device
from tests.contract.test_notification_outbox_contract import new_notification
from tests.integration.m2_support import memory_settings


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


def _registration(
    *,
    client_device_id: str = "phone-installation",
    routing_value: str = "0123456789abcdef",
) -> dict[str, object]:
    return {
        "client_device_id": client_device_id,
        "name": "Test iPhone",
        "kind": "mobile",
        "platform": "ios",
        "app_bundle_id": "com.veetbot.app",
        "push_provider": "apns",
        "push_token": routing_value,
        "push_environment": "sandbox",
        "muted_kinds": [],
    }


def test_api_and_domain_accept_a_surface_without_push_routing() -> None:
    request = DeviceRegistrationRequest(
        client_device_id="surface-installation",
        name="Unpaired surface",
        kind="surface",
        platform="telegram",
    )

    registration = request.registration()
    durable_values = device(token=None).model_dump()
    durable_values.update(
        {
            "client_device_id": registration.client_device_id,
            "name": registration.name,
            "kind": DeviceKind.SURFACE,
            "platform": registration.platform,
        }
    )

    assert registration.kind is DeviceKind.SURFACE
    assert registration.push_provider is None
    assert Device.model_validate(durable_values).push_provider is None


async def test_device_routes_cover_lifecycle_audit_scopes_and_test_enqueue() -> None:
    principal = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )
    settings = replace(
        memory_settings(),
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    async with build(
        settings=settings,
        storage="memory",
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with _client(composition) as client:
            invalid_registration = _registration()
            invalid_registration["push_provider"] = "surprise"
            invalid = await client.post("/v1/devices", json=invalid_registration)
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "device_validation_error"
            assert invalid.json()["error"]["details"]["reason"] == ("device.push_provider_unknown")

            created = await client.post(
                "/v1/devices",
                json=_registration(),
                headers={"Idempotency-Key": "phone-registration"},
            )
            assert created.status_code == 201, created.text
            device = created.json()
            device_id = UUID(device["id"])
            assert "push_token" not in device
            assert device["push_token_fingerprint"] == "9f9f51"

            replay = await client.post(
                "/v1/devices",
                json=_registration(),
                headers={"Idempotency-Key": "phone-registration"},
            )
            assert replay.status_code == 200
            assert replay.json()["id"] == str(device_id)

            mismatched_replay = await client.post(
                "/v1/devices",
                json=_registration(routing_value="different-token"),
                headers={"Idempotency-Key": "phone-registration"},
            )
            assert mismatched_replay.status_code == 409
            assert mismatched_replay.json()["error"]["details"]["reason"] == (
                "device.idempotency_mismatch"
            )

            refreshed = await client.post(
                "/v1/devices",
                json=_registration(routing_value="fedcba9876543210"),
            )
            assert refreshed.status_code == 200
            assert refreshed.json()["id"] == str(device_id)
            assert refreshed.json()["push_token_fingerprint"] != "9f9f51"

            late_replay = await client.post(
                "/v1/devices",
                json=_registration(),
                headers={"Idempotency-Key": "phone-registration"},
            )
            assert late_replay.status_code == 200
            assert late_replay.json() == device

            second = await client.post(
                "/v1/devices",
                json=_registration(
                    client_device_id="tablet-installation",
                    routing_value="aabbccdd",
                ),
            )
            assert second.status_code == 201
            second_device_id = UUID(second.json()["id"])
            listing = await client.get("/v1/devices", params={"limit": 1})
            assert listing.status_code == 200
            assert len(listing.json()["items"]) == 1
            assert listing.json()["next_cursor"] is not None
            next_page = await client.get(
                "/v1/devices",
                params={"limit": 1, "cursor": listing.json()["next_cursor"]},
            )
            assert len(next_page.json()["items"]) == 1

            test_push = await client.post(
                f"/v1/devices/{device_id}/test-notification",
                headers={"Idempotency-Key": "physical-setup-check"},
            )
            assert test_push.status_code == 202
            replayed_push = await client.post(
                f"/v1/devices/{device_id}/test-notification",
                headers={"Idempotency-Key": "physical-setup-check"},
            )
            assert replayed_push.status_code == 200

            inbox = await client.get("/v1/notifications", params={"limit": 1})
            assert inbox.status_code == 200
            assert inbox.json()["items"][0]["notification"]["kind"] == "test"
            assert inbox.json()["items"][0]["deliveries"] == []

            revoked = await client.post(f"/v1/devices/{device_id}/revoke")
            assert revoked.status_code == 200
            assert revoked.json()["status"] == "revoked"
            assert revoked.json()["push_token_fingerprint"] is None

            transport = FakePushTransport()
            dispatcher = NotificationDispatcher(
                uow_factory=composition.uow_factory,
                transport=transport,
                providers=frozenset({PushProvider.APNS}),
                clock=composition.clock,
                ids=composition.ids,
                claimant="notify-revocation-gate",
            )
            assert await dispatcher.run_once() == 1
            assert transport.calls == []

            invalidation_notice = new_notification(
                notification_id=UUID(int=88012),
                dedupe_key=f"device.test:{second_device_id}:device-lifecycle-invalidation",
                created_at=composition.clock.now(),
            ).model_copy(
                update={
                    "tenant_id": principal.tenant_id,
                    "principal_id": principal.principal_id,
                }
            )
            async with composition.uow_factory() as uow:
                assert await uow.notification_outbox.enqueue(invalidation_notice) is not None
            invalidating_transport = FakePushTransport(
                [
                    PushOutcome(
                        outcome=DeliveryOutcome.UNREGISTERED,
                        provider_reason="Unregistered",
                    )
                ]
            )
            invalidating_dispatcher = NotificationDispatcher(
                uow_factory=composition.uow_factory,
                transport=invalidating_transport,
                providers=frozenset({PushProvider.APNS}),
                clock=composition.clock,
                ids=composition.ids,
                claimant="notify-invalidation-gate",
            )
            assert await invalidating_dispatcher.run_once() == 1

            deleted = await client.delete(f"/v1/devices/{device_id}")
            assert deleted.status_code == 204
            missing = await client.get(f"/v1/devices/{device_id}")
            assert missing.status_code == 404

        async with composition.uow_factory() as uow:
            events = await uow.process_events.list()
            lifecycle = [event for event in events if event.event_type.startswith("device.")]
            assert [event.event_type for event in lifecycle] == [
                "device.registered",
                "device.push_token_updated",
                "device.registered",
                "device.revoked",
                "device.push_token_invalidated",
                "device.deleted",
            ]
            assert all("push_token" not in event.payload for event in lifecycle)
            assert all("token_fingerprint" in event.payload for event in lifecycle)
            targets = await uow.devices.push_targets(
                principal.tenant_id,
                principal.principal_id,
                NotificationKind.TEST,
            )
            assert all(target.device_id != device_id for target in targets)

        reader = principal.model_copy(update={"scopes": {"device.read"}}, deep=True)
        async with _client(composition, principal=reader) as read_only:
            denied = await read_only.post("/v1/devices", json=_registration())
            assert denied.status_code == 403

        foreign = principal.model_copy(
            update={"tenant_id": "other", "principal_id": "other-user"},
            deep=True,
        )
        async with _client(composition, principal=foreign) as stranger:
            hidden = await stranger.get(f"/v1/devices/{second.json()['id']}")
            assert hidden.status_code == 404

        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        all_routes = [
            nested
            for route in app.routes
            for nested in (
                route.original_router.routes if hasattr(route, "original_router") else (route,)
            )
        ]
        routes = [
            route
            for route in all_routes
            if isinstance(route, APIRoute)
            and (route.path.startswith("/v1/devices") or route.path == "/v1/notifications")
        ]
        assert len(routes) == 7
        assert {
            (method, (route.openapi_extra or {})["required_scope"])
            for route in routes
            for method in (route.methods or set())
        } == {
            ("POST", "device.write"),
            ("GET", "device.read"),
            ("DELETE", "device.write"),
            ("GET", "notification.read"),
        }


async def test_notification_http_surface_and_production_are_default_off() -> None:
    async with build(settings=memory_settings(), storage="memory") as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        assert not [
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and (route.path.startswith("/v1/devices") or route.path == "/v1/notifications")
        ]
        assert composition.executor._notification_producer is None
        async with composition.uow_factory() as uow:
            assert await uow.notification_outbox.list(composition.principal, limit=10) == []

    production = replace(
        memory_settings(),
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        auth_token=SecretStr("test-api-bearer"),
        sandbox=SandboxMechanism.GVISOR,
        execution_service_socket=Path("/run/veetbot/execution.sock"),
        auth_tenant_id="tenant-a",
        auth_principal_id="principal-a",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    with pytest.raises(ConfigurationError, match="production notifications require PostgreSQL"):
        async with build(settings=production, storage="memory"):
            pass


async def test_offline_inbox_paginates_every_kind_with_delivery_outcomes() -> None:
    principal = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )
    settings = replace(
        memory_settings(),
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    async with build(
        settings=settings,
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        owned_device = device().model_copy(
            update={"tenant_id": principal.tenant_id, "principal_id": principal.principal_id}
        )
        inserted: list[NewNotification] = []
        async with composition.uow_factory() as uow:
            await uow.devices.upsert(owned_device, principal)
            for offset, kind in enumerate(NotificationKind, start=1):
                notification = _notification(kind, offset, principal)
                assert await uow.notification_outbox.enqueue(notification) is not None
                inserted.append(notification)
            await uow.notification_outbox.record_delivery(
                NotificationDelivery(
                    id=UUID(int=9999),
                    notification_id=inserted[0].id,
                    device_id=owned_device.id,
                    attempt=1,
                    outcome=DeliveryOutcome.DELIVERED,
                    provider_id="provider-attempt",
                    attempted_at=NOW + timedelta(minutes=1),
                )
            )

        recovered: list[dict[str, Any]] = []
        cursor: str | None = None
        async with _client(composition) as client:
            for _page_number in range(len(NotificationKind) + 1):
                response = await client.get(
                    "/v1/notifications",
                    params={"limit": 3, **({"cursor": cursor} if cursor else {})},
                )
                assert response.status_code == 200, response.text
                page = response.json()
                recovered.extend(page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            else:
                pytest.fail("notification pagination did not terminate within the page bound")

        assert {item["notification"]["kind"] for item in recovered} == {
            kind.value for kind in NotificationKind
        }
        delivered = next(
            item for item in recovered if item["notification"]["id"] == str(inserted[0].id)
        )
        assert delivered["deliveries"][0]["outcome"] == "delivered"
        assert delivered["deliveries"][0]["provider_id"] == "provider-attempt"


def _notification(kind: NotificationKind, offset: int, principal: Principal) -> NewNotification:
    notification_id = UUID(int=7000 + offset)
    session_id = UUID(int=7100 + offset)
    run_id = UUID(int=7200 + offset)
    schedule_id = UUID(int=7300 + offset)
    occurrence_id = UUID(int=7400 + offset)
    payload: dict[str, object] = {
        "kind": kind.value,
        "title": NOTIFICATION_TITLES[kind],
        "notification_id": notification_id,
    }
    references: dict[str, object] = {}
    if kind is NotificationKind.APPROVAL_REQUESTED:
        references = {
            "session_id": session_id,
            "run_id": run_id,
            "approval_id": UUID(int=7500 + offset),
        }
        payload["status"] = RunStatus.WAITING_FOR_APPROVAL.value
    elif kind is NotificationKind.QUESTION_ASKED:
        references = {
            "session_id": session_id,
            "run_id": run_id,
            "question_id": UUID(int=7600 + offset),
        }
        payload["status"] = RunStatus.WAITING_FOR_USER.value
    elif kind is NotificationKind.RUN_FAILED:
        references = {"session_id": session_id, "run_id": run_id}
        payload["status"] = RunStatus.FAILED.value
    elif kind is NotificationKind.SCHEDULE_RUN_FINISHED:
        references = {
            "session_id": session_id,
            "run_id": run_id,
            "schedule_id": schedule_id,
            "occurrence_id": occurrence_id,
        }
        payload["status"] = RunStatus.COMPLETED.value
    elif kind is NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED:
        references = {"schedule_id": schedule_id, "occurrence_id": occurrence_id}
        payload["status"] = OccurrenceDisposition.MISSED.value
    elif kind is NotificationKind.DEVICE_INVOCATION:
        payload["status"] = "pending"
        payload["invocation_id"] = UUID(int=7700 + offset)
        payload["device_id"] = UUID(int=7800 + offset)
    elif kind in {NotificationKind.OPS_ALERT, NotificationKind.OPS_RECOVERED}:
        payload.update(
            {
                "signal": "database",
                "severity": (
                    NotificationSeverity.RECOVERED.value
                    if kind is NotificationKind.OPS_RECOVERED
                    else NotificationSeverity.CRITICAL.value
                ),
                "reason_code": "ops.database_unavailable",
                "release_id": "test-release",
            }
        )
    payload.update(references)
    created_at = NOW + timedelta(seconds=offset)
    return NewNotification.model_validate(
        {
            "id": notification_id,
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
            "kind": kind.value,
            "dedupe_key": f"offline:{kind.value}:{offset}",
            **references,
            "payload": NotificationPayload.model_validate(payload),
            "priority": 5,
            "expires_at": created_at + timedelta(hours=24),
            "next_attempt_at": created_at,
            "created_at": created_at,
        }
    )
