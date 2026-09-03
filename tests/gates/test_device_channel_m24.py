"""Milestone 24 device-channel and SMS gates."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.device_channel import FakeDeviceChannel, PushWakeDeviceChannel
from agent_core.adapters.push import FakePushTransport
from agent_core.api import create_app
from agent_core.application.notification_dispatcher import NotificationDispatcher
from agent_core.application.notification_producer import NotificationProducer
from agent_core.bootstrap import Composition, build
from agent_core.config import ConfigurationError, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.devices import (
    DeviceCapability,
    DeviceInvocation,
    DeviceInvocationStatus,
    DeviceKind,
    DeviceRegistration,
    DeviceStatus,
    PushEnvironment,
    PushProvider,
)
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.policies import (
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.tools import ToolOutcomeStatus, ToolSource
from agent_core.ports.policies import PolicyEngine
from agent_core.runtime.worker import MaintenanceWorker
from agent_core.tools.device_tools import DEVICE_SMS_SEND_TOOL_NAME
from agent_core.tools.registry import RegisteredTool
from tests.contract.support import SHIPPED_INVOCATION_TIMEOUT_SECONDS, tool_context
from tests.contract.test_device_registry_contract import device
from tests.integration.m2_support import PRINCIPAL, memory_settings

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002d0")
FOREIGN_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002d1")
REVOKED_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002d2")
SIBLING_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002d9")
UNKNOWN_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002d3")
INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002d4")
SECOND_INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002d5")
THIRD_INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002d6")
FOURTH_INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002d7")
RUN_ID = UUID("00000000-0000-0000-0000-0000000002d8")

TOOL_NAME = DeviceCapability.SMS_SEND.value
SENDER = "+15555550123"
INGEST_BODY = "Marzipan needs feeding at six."
SEND_BODY = "Feeding at six, thank you."
CREDENTIAL_BODY = "token: abc123"  # noqa: S105 - hardline fixture, never a real secret
SEND_ARGUMENTS: dict[str, Any] = {"recipient": SENDER, "body": SEND_BODY}
RETRY_DELAYS = (30, 120, 600, 3600)


# --- shared fixtures --------------------------------------------------------


def _settings(**updates: object) -> Settings:
    """The two paired device flags, which is the only way the surface composes."""

    return replace(
        memory_settings(),
        device_channel_enabled=True,
        device_sms_enabled=True,
        **updates,  # type: ignore[arg-type]
    )


def _notifying_settings() -> Settings:
    """Device flags plus the notification pair the real push-wake channel needs."""

    return _settings(notification_api_enabled=True, notification_dispatch_enabled=True)


def _replies(count: int) -> FakeModelScript:
    return FakeModelScript(turns=[ScriptedTurn(text="Acknowledged.") for _ in range(count)])


def _send_turn(body: str, *, call_id: str = "send-sms") -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name=DEVICE_SMS_SEND_TOOL_NAME,
                arguments={"recipient": SENDER, "body": body},
                call_id=call_id,
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def _send_script(body: str) -> FakeModelScript:
    return FakeModelScript(turns=[_send_turn(body), ScriptedTurn(text="Done.")])


def _fake_channel(
    clock: FixedClock,
    *,
    status: DeviceInvocationStatus = DeviceInvocationStatus.SENT,
) -> FakeDeviceChannel:
    return FakeDeviceChannel(
        clock=clock,
        capabilities={DEVICE_ID: frozenset({TOOL_NAME})},
        owners={DEVICE_ID: PRINCIPAL},
        default_status=status,
    )


async def _seed_device(
    composition: Composition,
    *,
    device_id: UUID = DEVICE_ID,
    client_device_id: str = "owner-iphone",
    token: str | None = "push-token-a",
    principal_id: str | None = None,
    capabilities: frozenset[str] = frozenset({TOOL_NAME}),
    status: DeviceStatus = DeviceStatus.ACTIVE,
) -> None:
    """Seed one durable device row under the composition's own principal."""

    owner = composition.principal
    owned = device(
        device_id=device_id,
        client_device_id=client_device_id,
        token=token,
        capabilities=capabilities,
    ).model_copy(
        update={
            "tenant_id": owner.tenant_id,
            "principal_id": principal_id or owner.principal_id,
            "status": status,
            "revoked_at": None if status is DeviceStatus.ACTIVE else NOW,
        }
    )
    writer = (
        owner
        if principal_id is None
        else owner.model_copy(update={"principal_id": principal_id}, deep=True)
    )
    async with composition.uow_factory() as uow:
        await uow.devices.upsert(owned, writer)


def _pending(
    invocation_id: UUID,
    *,
    tenant_id: str,
    created_at: datetime,
    device_id: UUID = DEVICE_ID,
) -> DeviceInvocation:
    return DeviceInvocation(
        id=invocation_id,
        tenant_id=tenant_id,
        device_id=device_id,
        run_id=RUN_ID,
        tool_name=TOOL_NAME,
        arguments=dict(SEND_ARGUMENTS),
        status=DeviceInvocationStatus.PENDING,
        created_at=created_at,
    )


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


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


def _registered(composition: Composition) -> RegisteredTool:
    return cast(
        RegisteredTool,
        composition.tool_pipeline._registry.get(
            DEVICE_SMS_SEND_TOOL_NAME,
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
        ),
    )


async def _drive_one_turn(composition: Composition, prompt: str = "ready?") -> None:
    """Run one throwaway turn, because attach happens while a step is planned."""

    await composition.runs.wait_terminal(await composition.runs.submit(prompt))


def _record_device_decisions(
    composition: Composition,
    recording: list[tuple[UUID, PolicyDecisionType]],
) -> None:
    """Capture, per run, every decision the engine reaches for the device send."""

    inner = composition.tool_pipeline._policy

    class _Recording:
        async def evaluate(
            self,
            action: ProposedAction,
            principal: Principal,
            run: Run,
        ) -> PolicyDecision:
            decision = await inner.evaluate(action, principal, run)
            if action.name == DEVICE_SMS_SEND_TOOL_NAME:
                recording.append((action.run_id, decision.decision))
            return decision

    composition.tool_pipeline._policy = cast(PolicyEngine, _Recording())


def _decisions_for(
    recording: list[tuple[UUID, PolicyDecisionType]],
    run_id: UUID,
) -> list[PolicyDecisionType]:
    return [decision for recorded_run, decision in recording if recorded_run == run_id]


async def _ingest(
    composition: Composition,
    *,
    body: str = INGEST_BODY,
    received_at: datetime = NOW,
) -> Any:
    return await composition.services.device_ingest.ingest(
        composition.principal,
        DEVICE_ID,
        channel="sms",
        sender=SENDER,
        body=body,
        received_at=received_at,
    )


# --- gate.device.capability_registration ------------------------------------


async def test_gate_capability_registration() -> None:
    """A device tool exists exactly while a live device declares its capability."""

    clock = FixedClock(NOW)
    registration = DeviceRegistration(
        client_device_id="owner-iphone",
        name="Owner iPhone",
        kind=DeviceKind.MOBILE,
        platform="ios",
        app_bundle_id="com.example.veetbot",
        push_provider=PushProvider.APNS,
        push_token=SecretStr("push-token-a"),
        push_environment=PushEnvironment.SANDBOX,
    )
    declaring = registration.model_copy(update={"capabilities": frozenset({TOOL_NAME})})
    async with build(
        settings=_settings(),
        script=_replies(5),
        clock=clock,
        sequential_ids=True,
        device_channel_override=_fake_channel(clock),
    ) as composition:
        devices = composition.services.devices
        principal = composition.principal

        plain = await devices.register(principal, registration)
        await _drive_one_turn(composition)
        with pytest.raises(NotFoundError):
            _registered(composition)

        declared = await devices.register(principal, declaring)
        await _drive_one_turn(composition)
        spec = _registered(composition).spec

        await devices.revoke(principal, declared.device.id)
        await _drive_one_turn(composition)
        with pytest.raises(NotFoundError):
            _registered(composition)

        await devices.register(principal, declaring)
        await _drive_one_turn(composition)
        revived = _registered(composition).spec

        await devices.delete(principal, declared.device.id)
        await _drive_one_turn(composition)
        with pytest.raises(NotFoundError):
            _registered(composition)

    assert plain.device.id == declared.device.id
    assert spec.source is ToolSource.DEVICE
    assert spec.target_kind == "device"
    assert spec.device_id == str(declared.device.id)
    assert spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert spec.required_scopes == {"device.write"}
    assert revived.device_id == spec.device_id


# --- gate.device.invocation_idempotent --------------------------------------


async def test_gate_invocation_idempotent() -> None:
    """A replayed invocation id resolves the first row and wakes the phone once."""

    clock = FixedClock(NOW)
    transport = FakePushTransport()
    async with build(
        settings=_notifying_settings(),
        script=_replies(1),
        clock=clock,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)
        channel = PushWakeDeviceChannel(
            uow_factory=composition.uow_factory,
            notification_producer=NotificationProducer(clock=clock, ids=composition.ids),
            clock=clock,
            invocation_timeout_seconds=4,
            poll_seconds=2,
        )

        first = await channel.invoke(
            device_id=DEVICE_ID,
            run_id=RUN_ID,
            invocation_id=INVOCATION_ID,
            tool_name=TOOL_NAME,
            arguments=dict(SEND_ARGUMENTS),
            principal=composition.principal,
        )
        replayed = await channel.invoke(
            device_id=DEVICE_ID,
            run_id=RUN_ID,
            invocation_id=INVOCATION_ID,
            tool_name=TOOL_NAME,
            arguments=dict(SEND_ARGUMENTS),
            principal=composition.principal,
        )

        async with composition.uow_factory() as uow:
            stored = await uow.device_invocations.get(INVOCATION_ID)
            queued = await uow.notification_outbox.list(composition.principal, limit=10)
        delivered = await NotificationDispatcher(
            uow_factory=composition.uow_factory,
            transport=transport,
            providers=frozenset({PushProvider.APNS}),
            clock=clock,
            ids=composition.ids,
            claimant="gate-device",
            batch_size=10,
            lease_seconds=30,
            retry_delays=RETRY_DELAYS,
        ).run_once()

    assert replayed == first
    assert stored == first
    assert stored.status is DeviceInvocationStatus.EXPIRED
    assert [notification.payload.invocation_id for notification in queued] == [INVOCATION_ID]
    assert delivered == 1
    assert len(transport.calls) == 1
    assert transport.calls[0][0].device_id == DEVICE_ID


# --- gate.device.foreign_device_denied --------------------------------------


async def test_gate_foreign_device_denied() -> None:
    """Only the invocation's own live device may see it or answer it."""

    clock = FixedClock(NOW)
    async with build(
        settings=_settings(),
        script=_replies(1),
        clock=clock,
        sequential_ids=True,
        device_channel_override=_fake_channel(clock),
    ) as composition:
        await _seed_device(composition)
        # The threat this gate actually names: every device in the deployment
        # authenticates as the same owner principal, so the owner's *second*
        # phone clears the route's principal-scoped presence check and is
        # refused only by the invocation's own device ownership.
        await _seed_device(
            composition,
            device_id=SIBLING_DEVICE_ID,
            client_device_id="second-installation",
            token="push-token-c",
        )
        await _seed_device(
            composition,
            device_id=FOREIGN_DEVICE_ID,
            client_device_id="foreign-installation",
            token="push-token-b",
            principal_id="someone-else",
        )
        await _seed_device(
            composition,
            device_id=REVOKED_DEVICE_ID,
            client_device_id="revoked-installation",
            token=None,
            status=DeviceStatus.REVOKED,
        )
        async with composition.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(
                    INVOCATION_ID,
                    tenant_id=composition.principal.tenant_id,
                    created_at=NOW,
                )
            )
        async with _client(composition) as client:
            unknown_fetch = await client.get(f"/v1/devices/{UNKNOWN_DEVICE_ID}/invocations")
            foreign_fetch = await client.get(f"/v1/devices/{FOREIGN_DEVICE_ID}/invocations")
            revoked_fetch = await client.get(f"/v1/devices/{REVOKED_DEVICE_ID}/invocations")
            sibling_fetch = await client.get(f"/v1/devices/{SIBLING_DEVICE_ID}/invocations")
            owner_fetch = await client.get(f"/v1/devices/{DEVICE_ID}/invocations")
            sibling_result = await client.post(
                f"/v1/devices/{SIBLING_DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
            foreign_result = await client.post(
                f"/v1/devices/{FOREIGN_DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
            revoked_result = await client.post(
                f"/v1/devices/{REVOKED_DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
        async with composition.uow_factory() as uow:
            untouched = await uow.device_invocations.get(INVOCATION_ID)

    # Another principal's device, and one this principal never registered, are
    # both simply absent: the route's presence read is principal scoped.
    assert unknown_fetch.status_code == 404
    assert foreign_fetch.status_code == 404
    assert foreign_result.status_code == 404
    assert revoked_fetch.status_code == 409
    assert revoked_fetch.json()["error"]["details"]["reason"] == "device_revoked"
    assert revoked_result.status_code == 409
    # The owner's own second phone is present and live, so presence admits it.
    # It still sees nothing it does not own and cannot answer for its sibling.
    assert sibling_fetch.status_code == 200
    assert sibling_fetch.json() == {"invocations": []}
    assert sibling_result.status_code == 404
    assert [row["id"] for row in owner_fetch.json()["invocations"]] == [str(INVOCATION_ID)]
    assert untouched.status is DeviceInvocationStatus.PENDING


# --- gate.device.no_send_without_result -------------------------------------


async def test_gate_no_send_without_result() -> None:
    """Every server-driven resolution expires; only a device post can mean sent."""

    clock = FixedClock(NOW)
    async with build(
        settings=_notifying_settings(),
        script=_replies(1),
        clock=clock,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)
        tenant_id = composition.principal.tenant_id

        # (1) The adapter's own bounded wait, with no device post at all.
        async with composition.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(INVOCATION_ID, tenant_id=tenant_id, created_at=clock.now())
            )
        adapter_resolved = await PushWakeDeviceChannel(
            uow_factory=composition.uow_factory,
            notification_producer=NotificationProducer(clock=clock, ids=composition.ids),
            clock=clock,
            invocation_timeout_seconds=4,
            poll_seconds=2,
        ).invoke(
            device_id=DEVICE_ID,
            run_id=RUN_ID,
            invocation_id=INVOCATION_ID,
            tool_name=TOOL_NAME,
            arguments=dict(SEND_ARGUMENTS),
            principal=composition.principal,
        )

        # (2) and (3) The two device-posted terminals the result route accepts.
        async with composition.uow_factory() as uow:
            created = await uow.device_invocations.create(
                _pending(SECOND_INVOCATION_ID, tenant_id=tenant_id, created_at=clock.now())
            )
            await uow.device_invocations.create(
                _pending(THIRD_INVOCATION_ID, tenant_id=tenant_id, created_at=clock.now())
            )
        async with _client(composition) as client:
            cancelled = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{SECOND_INVOCATION_ID}/result",
                json={"status": "cancelled"},
            )
            sent = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{THIRD_INVOCATION_ID}/result",
                json={"status": "sent"},
            )
            refused = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )

        # (4) The maintenance sweep, the other server-driven resolution.
        async with composition.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(FOURTH_INVOCATION_ID, tenant_id=tenant_id, created_at=clock.now())
            )
        clock.advance(timedelta(seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS + 1))
        await cast(MaintenanceWorker, composition.maintenance_factory()).run_once()

        async with composition.uow_factory() as uow:
            statuses = {
                "adapter_timeout": (await uow.device_invocations.get(INVOCATION_ID)).status,
                "device_cancelled": (await uow.device_invocations.get(SECOND_INVOCATION_ID)).status,
                "device_sent": (await uow.device_invocations.get(THIRD_INVOCATION_ID)).status,
                "maintenance_sweep": (
                    await uow.device_invocations.get(FOURTH_INVOCATION_ID)
                ).status,
            }

    assert created is not None
    assert created.status is DeviceInvocationStatus.PENDING
    assert adapter_resolved.status is DeviceInvocationStatus.EXPIRED
    assert cancelled.status_code == 200
    assert sent.status_code == 200
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "device_invocation_expired"
    # The property: no server-driven path reaches sent, and the only status
    # sent ever appears under is the one a device posted through its route.
    assert statuses == {
        "adapter_timeout": DeviceInvocationStatus.EXPIRED,
        "device_cancelled": DeviceInvocationStatus.CANCELLED,
        "device_sent": DeviceInvocationStatus.SENT,
        "maintenance_sweep": DeviceInvocationStatus.EXPIRED,
    }
    assert [name for name, status in statuses.items() if status is DeviceInvocationStatus.SENT] == [
        "device_sent"
    ]


# --- gate.device.offline_outcome --------------------------------------------


async def test_gate_offline_outcome() -> None:
    """An unanswered invocation expires and reaches the model as unavailable."""

    clock = FixedClock(NOW)
    async with build(
        settings=_notifying_settings(),
        script=_send_script(SEND_BODY),
        clock=clock,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)
        run_id = await composition.runs.submit("Text the sitter.")
        completed = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            [wake] = await uow.notification_outbox.list(composition.principal, limit=10)
            invocation_id = wake.payload.invocation_id
            assert invocation_id is not None
            row = await uow.device_invocations.get(invocation_id)
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert completed.status is RunStatus.COMPLETED
    assert clock.now() >= NOW + timedelta(seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS)
    assert row.status is DeviceInvocationStatus.EXPIRED
    [call] = [item for item in invocations if item.tool_name == DEVICE_SMS_SEND_TOOL_NAME]
    assert call.outcome is not None
    assert call.outcome.status is ToolOutcomeStatus.UNAVAILABLE
    assert call.outcome.reason_code == "tool.device_offline"


# --- gate.device.untrusted_output -------------------------------------------


async def test_gate_untrusted_output() -> None:
    """Registration forces the device tool's output trust, and the result carries it."""

    clock = FixedClock(NOW)
    async with build(
        settings=_settings(),
        script=_send_script(SEND_BODY),
        clock=clock,
        sequential_ids=True,
        device_channel_override=_fake_channel(clock),
    ) as composition:
        await _seed_device(composition)
        run_id = await composition.runs.submit("Text the sitter.")
        completed = await composition.runs.wait_terminal(run_id)
        spec = _registered(composition).spec
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert completed.status is RunStatus.COMPLETED
    assert spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    [call] = [item for item in invocations if item.tool_name == DEVICE_SMS_SEND_TOOL_NAME]
    assert call.structured_result is not None
    assert call.structured_result["status"] == "sent"
    assert call.result_item is not None
    assert call.result_item.trust is TrustLevel.EXTERNAL_UNTRUSTED


# --- gate.device.presence_revalidated ---------------------------------------


async def test_gate_presence_revalidated() -> None:
    """Every device-channel action rereads presence, so a revoked device fails next."""

    clock = FixedClock(NOW)
    async with build(
        settings=_notifying_settings(),
        script=_replies(1),
        clock=clock,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)
        await _drive_one_turn(composition)
        registered = _registered(composition)
        async with composition.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(
                    INVOCATION_ID,
                    tenant_id=composition.principal.tenant_id,
                    created_at=clock.now(),
                )
            )

        async with _client(composition) as client:
            fetched = await client.get(f"/v1/devices/{DEVICE_ID}/invocations")
            await composition.services.devices.revoke(composition.principal, DEVICE_ID)
            answered = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
            refetched = await client.get(f"/v1/devices/{DEVICE_ID}/invocations")

        # The registration survives until the next attach, so the invoke path is
        # the one revalidating presence here rather than the registry.
        result = await registered.execute(
            dict(SEND_ARGUMENTS),
            replace(tool_context(), principal=composition.principal),
        )
        async with composition.uow_factory() as uow:
            unresolved = await uow.device_invocations.get(INVOCATION_ID)

    assert [row["id"] for row in fetched.json()["invocations"]] == [str(INVOCATION_ID)]
    assert answered.status_code == 409
    assert answered.json()["error"]["details"]["reason"] == "device_revoked"
    assert refetched.status_code == 409
    assert unresolved.status is DeviceInvocationStatus.PENDING
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.reason_code == "tool.device_offline"


# --- gate.device.outbound_secret_scan ---------------------------------------


async def test_gate_outbound_secret_scan() -> None:
    """A credential-shaped body is denied before any invocation row exists."""

    clock = FixedClock(NOW)
    channel = _fake_channel(clock)
    async with build(
        settings=_settings(),
        script=_send_script(CREDENTIAL_BODY),
        clock=clock,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        run_id = await composition.runs.submit("Text the sitter my API token.")
        completed = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(completed.session_id, 0, composition.principal)
            pending = await uow.device_invocations.list_pending_for_device(
                DEVICE_ID, now=clock.now()
            )

    denials = [event for event in events if event.event_type == "tool.call.denied"]
    assert [event.payload["reason_code"] for event in denials] == [
        "policy.hardline.secret_exfiltration"
    ]
    assert channel.invocations == []
    assert pending == []


# --- gate.device.ingest_idempotent ------------------------------------------


async def test_gate_ingest_idempotent() -> None:
    """A replayed digest stores one receipt and seeds exactly one run."""

    clock = FixedClock(NOW)
    async with build(
        settings=_settings(),
        script=_replies(4),
        clock=clock,
        sequential_ids=True,
        device_channel_override=_fake_channel(clock),
    ) as composition:
        await _seed_device(composition)

        first = await _ingest(composition)
        replayed = await _ingest(composition)
        async with composition.uow_factory() as uow:
            receipts = await uow.device_ingest.count_for_utc_day(DEVICE_ID, "sms", day=NOW.date())
            events = await uow.events.list_after(first.session_id, 0, composition.principal)

    assert first.duplicate is False
    assert replayed == first.model_copy(update={"duplicate": True})
    assert receipts == 1
    assert [event.event_type for event in events].count("user.message.created") == 1
    assert [event.event_type for event in events].count("run.queued") == 1


# --- gate.device.untrusted_triage_routing -----------------------------------


async def test_gate_untrusted_triage_routing() -> None:
    """The seeded triage turn is untrusted, so it cannot reach a plain allow."""

    clock = FixedClock(NOW)
    channel = _fake_channel(clock)
    script = FakeModelScript(
        turns=[
            _send_turn(SEND_BODY, call_id="triage-reply"),
            _send_turn(SEND_BODY, call_id="owner-reply"),
            ScriptedTurn(text="Done."),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        clock=clock,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        decisions: list[tuple[UUID, PolicyDecisionType]] = []
        _record_device_decisions(composition, decisions)

        ingested = await _ingest(composition)
        parked = await composition.runs.get(ingested.run_id)
        approvals = await composition.approvals.list_pending(run_id=ingested.run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(ingested.session_id, 0, composition.principal)

        # The contrast: the identical call from an owner-authored turn is the
        # plain allow the tool rule grants, so the escalation above is the
        # untrusted origin and nothing else.
        owner_run = await composition.runs.submit("Text the sitter for me.")
        await composition.runs.wait_terminal(owner_run)

    [seed] = [event for event in events if event.event_type == "user.message.created"]
    assert seed.payload["trust"] == TrustLevel.EXTERNAL_UNTRUSTED.value
    assert _decisions_for(decisions, ingested.run_id) == [PolicyDecisionType.REQUIRE_APPROVAL]
    assert parked.status is RunStatus.WAITING_FOR_APPROVAL
    assert [approval.tool_name for approval in approvals] == [DEVICE_SMS_SEND_TOOL_NAME]
    assert _decisions_for(decisions, owner_run) == [PolicyDecisionType.ALLOW]
    # The untrusted turn reached no device at all; only the owner's turn did.
    assert [invocation.run_id for invocation in channel.invocations] == [owner_run]


# --- gate.device.no_body_in_logs --------------------------------------------


async def test_gate_no_body_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """No message body reaches a log line, and only the seed event carries one."""

    caplog.set_level(logging.DEBUG)
    clock = FixedClock(NOW)
    channel = _fake_channel(clock)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(text="Triaged."),
            _send_turn(SEND_BODY),
            ScriptedTurn(text="Done."),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        clock=clock,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        ingested = await _ingest(composition)
        run_id = await composition.runs.submit("Text the sitter for me.")
        await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            triage_events = await uow.events.list_after(
                ingested.session_id, 0, composition.principal
            )
            process_events = await uow.process_events.list()
    emitted = [record for record in caplog.records if record.name.startswith("agent_core")]

    # Control: the capture is live for the whole platform logger tree, so an
    # absent body below is an observation rather than a dead fixture.
    logging.getLogger("agent_core.gate_probe").debug("device gate capture probe")
    probes = [record for record in caplog.records if record.name == "agent_core.gate_probe"]

    assert [record.getMessage() for record in probes] == ["device gate capture probe"]
    # Ingest and invoke log nothing at all, which is the strongest form of the
    # claim; the substring checks below hold for whatever any of them emits.
    assert emitted == []
    logged = caplog.text + "".join(record.getMessage() for record in caplog.records)
    assert INGEST_BODY not in logged
    assert SEND_BODY not in logged
    assert SENDER not in logged
    carriers = [event for event in triage_events if INGEST_BODY in str(event.payload)]
    assert [event.event_type for event in carriers] == ["user.message.created"]
    assert not [event for event in process_events if INGEST_BODY in str(event.payload)]
    assert not [event for event in process_events if SEND_BODY in str(event.payload)]
    assert [invocation.arguments["body"] for invocation in channel.invocations] == [SEND_BODY]


# --- gate.device.default_off ------------------------------------------------


async def test_gate_default_off() -> None:
    """With either flag unset the surface refuses to exist at all."""

    clock = FixedClock(NOW)
    for half_enabled in (
        replace(memory_settings(), device_channel_enabled=True),
        replace(memory_settings(), device_sms_enabled=True),
    ):
        with pytest.raises(ConfigurationError, match="device channel and SMS"):
            async with build(
                settings=half_enabled,
                script=_replies(1),
                clock=FixedClock(NOW),
                sequential_ids=True,
            ):
                pass

    async with build(
        settings=memory_settings(),
        script=_send_script(SEND_BODY),
        clock=clock,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        async with _client(composition) as client:
            fetched = await client.get(f"/v1/devices/{DEVICE_ID}/invocations")
            answered = await client.post(
                f"/v1/devices/{DEVICE_ID}/invocations/{INVOCATION_ID}/result",
                json={"status": "sent"},
            )
            ingested = await client.post(
                f"/v1/devices/{DEVICE_ID}/messages",
                json={
                    "channel": "sms",
                    "sender": SENDER,
                    "body": INGEST_BODY,
                    "received_at": NOW.isoformat(),
                },
            )
        run_id = await composition.runs.submit("Text the sitter.")
        await composition.runs.wait_terminal(run_id)
        with pytest.raises(NotFoundError):
            _registered(composition)
        async with composition.uow_factory() as uow:
            queued = await uow.notification_outbox.list(composition.principal, limit=10)
            pending = await uow.device_invocations.list_pending_for_device(
                DEVICE_ID, now=clock.now()
            )

    assert _device_channel_routes(app) == []
    assert fetched.status_code == 404
    assert answered.status_code == 404
    assert ingested.status_code == 404
    assert queued == []
    assert pending == []


async def test_sms_tool_is_advertised_only_when_the_channel_can_wake() -> None:
    """The agent lists the SMS tool exactly when the device runtime registers it.

    Device flags without notification dispatch or an injected channel leave
    the runtime unset; advertising the tool anyway sends every call into an
    unknown-tool denial. The advertisement follows the same readiness rule.
    """

    async def enabled_tools(composition: object) -> set[str]:
        session_id = await composition.sessions.create()  # type: ignore[attr-defined]
        async with composition.uow_factory() as uow:  # type: ignore[attr-defined]
            session = await uow.sessions.get(session_id, composition.principal)  # type: ignore[attr-defined]
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)
        return set(agent.enabled_tools)

    dark_clock = FixedClock(NOW)
    async with build(
        settings=_settings(),
        script=_replies(1),
        clock=dark_clock,
        sequential_ids=True,
    ) as dark:
        assert DEVICE_SMS_SEND_TOOL_NAME not in await enabled_tools(dark)

    lit_clock = FixedClock(NOW)
    async with build(
        settings=_settings(),
        script=_replies(1),
        clock=lit_clock,
        sequential_ids=True,
        device_channel_override=_fake_channel(lit_clock),
    ) as lit:
        assert DEVICE_SMS_SEND_TOOL_NAME in await enabled_tools(lit)

    async with build(
        settings=_notifying_settings(),
        script=_replies(1),
        clock=FixedClock(NOW),
        sequential_ids=True,
    ) as notifying:
        assert DEVICE_SMS_SEND_TOOL_NAME in await enabled_tools(notifying)
