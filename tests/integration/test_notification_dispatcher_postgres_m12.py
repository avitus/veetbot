"""PostgreSQL claim-lease and accepted-send replay behavior."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.push import FakePushTransport
from agent_core.application.notification_dispatcher import DispatchProbe, NotificationDispatcher
from agent_core.bootstrap import Composition, build
from agent_core.domain.devices import PushProvider
from agent_core.domain.notifications import NotificationStatus
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.support import NOW
from tests.contract.test_device_registry_contract import device
from tests.contract.test_notification_outbox_contract import new_notification
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


async def _seed(composition: Composition) -> None:
    owner = composition.principal
    owned_device = device().model_copy(
        update={"tenant_id": owner.tenant_id, "principal_id": owner.principal_id}
    )
    owned_notification = new_notification().model_copy(
        update={"tenant_id": owner.tenant_id, "principal_id": owner.principal_id}
    )
    async with composition.uow_factory() as uow:
        await uow.devices.upsert(owned_device, owner)
        assert await uow.notification_outbox.enqueue(owned_notification) is not None


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

        replay = new_notification(
            notification_id=UUID(int=1301),
            dedupe_key="test:accepted-send-replay",
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
            == "test:accepted-send-replay"
        )
        async with composition.uow_factory() as uow:
            rows = await uow.notification_outbox.list(composition.principal, limit=10)
            notification = next(
                row for row in rows if row.dedupe_key == "test:accepted-send-replay"
            )
            assert notification.status is NotificationStatus.DISPATCHED
            assert notification.attempts == 2
            [delivery] = await uow.notification_outbox.list_deliveries(notification.id)
            assert delivery.attempt == 2
