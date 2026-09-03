"""PostgreSQL claim-lease and accepted-send replay behavior."""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.notification_wakeup import PostgresNotificationWakeup
from agent_core.adapters.push import FakePushTransport
from agent_core.application.notification_dispatcher import DispatchProbe, NotificationDispatcher
from agent_core.application.notification_producer import NotificationProducer
from agent_core.application.notification_worker import NotificationWorker
from agent_core.bootstrap import Composition, build, build_notification_worker
from agent_core.config import AuthMode, DeploymentMode, PushProviderKind, SandboxMechanism
from agent_core.domain.devices import (
    Device,
    DeviceInvocation,
    DeviceInvocationStatus,
    DeviceKind,
    DeviceStatus,
    PushProvider,
)
from agent_core.domain.notifications import NewNotification, NotificationStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.support import NOW
from tests.contract.test_device_registry_contract import DEVICE_ID, device
from tests.contract.test_notification_outbox_contract import NOTIFICATION_ID, new_notification
from tests.integration.m2_support import database_settings


def _dispatcher(
    *,
    factory: UnitOfWorkFactory,
    clock: Clock,
    ids: IdFactory,
    transport: FakePushTransport,
    claimant: str,
    probe: DispatchProbe | None = None,
) -> NotificationDispatcher:
    return NotificationDispatcher(
        uow_factory=factory,
        transport=transport,
        providers=frozenset({PushProvider.APNS}),
        clock=clock,
        ids=ids,
        claimant=claimant,
        batch_size=10,
        lease_seconds=30,
        retry_delays=(30, 120, 600, 3600),
        dispatch_probe=probe,
    )


def _targeted_notification(
    *,
    notification_id: UUID = NOTIFICATION_ID,
    key: str = "one",
    created_at: datetime = NOW,
) -> NewNotification:
    return new_notification(
        notification_id=notification_id,
        dedupe_key=f"device.test:{DEVICE_ID}:{key}",
        created_at=created_at,
    )


async def _seed(composition: Composition) -> None:
    owner = composition.principal
    owned_device = device().model_copy(
        update={"tenant_id": owner.tenant_id, "principal_id": owner.principal_id}
    )
    owned_notification = _targeted_notification().model_copy(
        update={"tenant_id": owner.tenant_id, "principal_id": owner.principal_id}
    )
    async with composition.uow_factory() as uow:
        await uow.devices.upsert(owned_device, owner)
        assert await uow.notification_outbox.enqueue(owned_notification) is not None


async def test_postgres_enqueue_wakes_only_after_transaction_commit() -> None:
    listener = PostgresNotificationWakeup(database_settings().database_url)
    try:
        waiting = asyncio.create_task(listener.wait(5))
        async with build(settings=database_settings(), storage="postgres") as composition:
            owned = _targeted_notification().model_copy(
                update={
                    "tenant_id": composition.principal.tenant_id,
                    "principal_id": composition.principal.principal_id,
                }
            )
            async with composition.uow_factory() as uow:
                assert await uow.notification_outbox.enqueue(owned) is not None
                await asyncio.sleep(0)
                assert not waiting.done()
            await asyncio.wait_for(waiting, timeout=1)
    finally:
        await listener.close()


async def test_postgres_claim_does_not_mistake_an_unrelated_devices_provider_for_the_named_target() -> (  # noqa: E501
    None
):
    """SQL twin of the in-memory claim-narrowing test: `_pending_target_exists`
    must narrow DEVICE_INVOCATION by its payload device_id exactly as it
    already narrows TEST by its dedupe key, so an unrelated device's own
    provider eligibility can never justify claiming a notification confined
    to a different, currently-unreachable device."""

    async with build(
        settings=database_settings(),
        storage="postgres",
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        owner = composition.principal
        named_id = UUID(int=0x4E0000)
        unrelated_id = UUID(int=0x4E0001)
        invocation_id = UUID(int=0x4E0002)
        run_id = UUID(int=0x4E0003)
        named = device(device_id=named_id, client_device_id="pg-target-device").model_copy(
            update={"tenant_id": owner.tenant_id, "principal_id": owner.principal_id}
        )
        unrelated = Device(
            id=unrelated_id,
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            client_device_id="pg-unrelated-surface",
            name="Unrelated surface",
            kind=DeviceKind.SURFACE,
            platform="telegram",
            app_bundle_id=None,
            push_provider=PushProvider.TELEGRAM,
            push_token=SecretStr("telegram-chat-ref"),  # noqa: S106
            push_environment=None,
            muted_kinds=frozenset(),
            status=DeviceStatus.ACTIVE,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        invocation = DeviceInvocation(
            id=invocation_id,
            tenant_id=owner.tenant_id,
            device_id=named_id,
            run_id=run_id,
            tool_name="device.sms.send",
            arguments={},
            status=DeviceInvocationStatus.PENDING,
            created_at=NOW,
        )
        producer = NotificationProducer(clock=composition.clock, ids=composition.ids)
        async with composition.uow_factory() as uow:
            await uow.devices.upsert(named, owner)
            await uow.devices.upsert(unrelated, owner)
            assert await producer.for_device_invocation(uow, invocation=invocation, device=named)

            claimed = await uow.notification_outbox.claim_due(
                NOW, 10, "notify-a", 30, frozenset({PushProvider.TELEGRAM})
            )
            assert claimed == []
            [row] = await uow.notification_outbox.list(owner, limit=10)

        assert row.status is NotificationStatus.PENDING
        assert row.attempts == 0
        assert row.claimed_by is None


async def test_lean_production_notification_role_constructs_without_app_credentials(
    tmp_path: Path,
) -> None:
    apns_key = tmp_path / "AuthKey_TEST.p8"
    apns_key.write_text("test APNs private key material", encoding="ascii")
    apns_key.chmod(0o600)
    settings = replace(
        database_settings(),
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        sandbox=SandboxMechanism.GVISOR,
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"notify"}),
        auth_scopes=PLATFORM_SCOPES,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
        push_provider=PushProviderKind.APNS,
        apns_key_file=apns_key,
        apns_key_id="KEYID",
        apns_team_id="TEAMID",
        apns_topic="com.veetbot.app",
    )
    async with build_notification_worker(
        settings=settings,
        transport=FakePushTransport(),
    ) as worker:
        assert isinstance(worker, NotificationWorker)
        dispatcher = cast(Any, worker._dispatch_once).__self__
        assert dispatcher._claimant == f"notify:{socket.gethostname()}:{os.getpid()}"
        async with dispatcher._uow_factory() as uow:
            assert {
                "approvals",
                "checkpoints",
                "devices",
                "notification_outbox",
                "process_events",
                "runs",
                "sessions",
            } <= set(vars(uow))
            assert not {
                "agents",
                "browser_profiles",
                "events",
                "mcp_servers",
                "skills",
                "usage",
            } & set(vars(uow))


async def test_postgres_concurrent_dispatch_and_accepted_send_replay_are_bounded() -> None:
    clock = FixedClock(NOW)
    ids = SequenceIdFactory()
    async with build(
        settings=database_settings(),
        storage="postgres",
        clock=clock,
        ids=ids,
    ) as composition:
        await _seed(composition)
        transport = FakePushTransport()

        results = await asyncio.gather(
            _dispatcher(
                factory=composition.uow_factory,
                clock=clock,
                ids=ids,
                transport=transport,
                claimant="notify-a",
            ).run_once(),
            _dispatcher(
                factory=composition.uow_factory,
                clock=clock,
                ids=ids,
                transport=transport,
                claimant="notify-b",
            ).run_once(),
        )

        assert sum(results) == 1
        assert len(transport.calls) == 1
        async with composition.uow_factory() as uow:
            [notification] = await uow.notification_outbox.list(composition.principal, limit=10)
            assert notification.status is NotificationStatus.DISPATCHED
            [delivery] = await uow.notification_outbox.list_deliveries(notification.id)
            assert delivery.attempt == 1

        replay = _targeted_notification(
            notification_id=UUID(int=1301),
            key="accepted-send-replay",
            created_at=clock.now(),
        ).model_copy(
            update={
                "tenant_id": composition.principal.tenant_id,
                "principal_id": composition.principal.principal_id,
            }
        )
        async with composition.uow_factory() as uow:
            assert await uow.notification_outbox.enqueue(replay) is not None
        replay_transport = FakePushTransport()

        def crash(_boundary: str) -> None:
            raise RuntimeError("injected crash after transport accept")

        with pytest.raises(RuntimeError, match="injected crash"):
            await _dispatcher(
                factory=composition.uow_factory,
                clock=clock,
                ids=ids,
                transport=replay_transport,
                claimant="notify-a",
                probe=crash,
            ).run_once()

        clock.advance(timedelta(seconds=31))
        assert (
            await _dispatcher(
                factory=composition.uow_factory,
                clock=clock,
                ids=ids,
                transport=replay_transport,
                claimant="notify-b",
            ).run_once()
            == 1
        )
        assert len(replay_transport.calls) == 2
        assert (
            replay_transport.calls[0][1].dedupe_key
            == replay_transport.calls[1][1].dedupe_key
            == replay.dedupe_key
        )
        async with composition.uow_factory() as uow:
            rows = await uow.notification_outbox.list(composition.principal, limit=10)
            notification = next(row for row in rows if row.dedupe_key == replay.dedupe_key)
            assert notification.status is NotificationStatus.DISPATCHED
            assert notification.attempts == 2
            [delivery] = await uow.notification_outbox.list_deliveries(notification.id)
            assert delivery.attempt == 2
