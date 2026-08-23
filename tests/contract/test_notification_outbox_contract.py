"""Shared notification-outbox contract."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.notifications import (
    InMemoryDeviceRegistry,
    InMemoryNotificationOutbox,
)
from agent_core.domain.agents import Principal
from agent_core.domain.devices import Device, DeviceKind, PushProvider
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.notifications import (
    DeliveryOutcome,
    NewNotification,
    NotificationCursor,
    NotificationDelivery,
    NotificationKind,
    NotificationPayload,
    NotificationStatus,
)
from agent_core.ports.devices import DeviceRegistry
from agent_core.ports.notifications import NotificationOutbox
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT, principal
from tests.contract.test_device_registry_contract import DEVICE_ID, device

NOTIFICATION_ID = UUID("00000000-0000-0000-0000-000000000312")


def new_notification(
    *,
    notification_id: UUID = NOTIFICATION_ID,
    principal_id: str = PRINCIPAL_ID,
    dedupe_key: str = "test:one",
    created_at: datetime = NOW,
) -> NewNotification:
    return NewNotification(
        id=notification_id,
        tenant_id=TENANT,
        principal_id=principal_id,
        kind=NotificationKind.TEST,
        dedupe_key=dedupe_key,
        payload=NotificationPayload(
            kind=NotificationKind.TEST,
            title="Test notification",
            notification_id=notification_id,
        ),
        priority=5,
        next_attempt_at=created_at,
        created_at=created_at,
    )


async def assert_notification_enqueue_deduplicates_and_lists_by_principal(
    outbox: NotificationOutbox,
) -> None:
    first = new_notification()
    enqueued = await outbox.enqueue(first)
    assert enqueued is not None
    assert enqueued.status is NotificationStatus.PENDING
    assert enqueued.attempts == 0
    repeated = new_notification(
        notification_id=UUID(int=first.id.int + 1),
        dedupe_key=first.dedupe_key,
    )
    assert await outbox.enqueue(repeated) is None

    stranger = Principal(
        tenant_id=TENANT,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )
    other = new_notification(
        notification_id=UUID(int=NOTIFICATION_ID.int + 2),
        principal_id=stranger.principal_id,
        dedupe_key="test:other",
    )
    assert await outbox.enqueue(other) is not None
    assert await outbox.list(principal(), limit=10) == [enqueued]
    assert len(await outbox.list(stranger, limit=10)) == 1


async def assert_notification_claim_settle_and_pagination(outbox: NotificationOutbox) -> None:
    values = [
        new_notification(
            notification_id=UUID(int=NOTIFICATION_ID.int + offset),
            dedupe_key=f"test:{offset}",
            created_at=NOW + timedelta(seconds=offset),
        )
        for offset in range(3)
    ]
    for value in values:
        assert await outbox.enqueue(value) is not None

    backlog = await outbox.list_pending_older_than(
        NOW + timedelta(seconds=3),
        1,
    )
    assert [notification.id for notification in backlog] == [values[0].id]

    claimed = await outbox.claim_due(
        NOW + timedelta(seconds=3),
        1,
        "dispatcher-a",
        30,
        frozenset({PushProvider.APNS}),
    )
    assert len(claimed) == 1
    assert claimed[0].attempts == 1
    assert claimed[0].claimed_by == "dispatcher-a"
    await outbox.settle(claimed[0].id, NotificationStatus.DISPATCHED, None)

    page = await outbox.list(principal(), limit=2)
    assert [item.id for item in page] == [values[2].id, values[1].id]
    cursor = NotificationCursor(created_at=page[-1].created_at, id=page[-1].id)
    assert [item.id for item in await outbox.list(principal(), limit=2, cursor=cursor)] == [
        values[0].id
    ]
    settled = [
        item for item in await outbox.list(principal(), limit=10) if item.id == claimed[0].id
    ]
    assert len(settled) == 1
    assert settled[0].status is NotificationStatus.DISPATCHED
    assert settled[0].settled_at is not None


async def assert_notification_claim_is_partitioned_by_provider(
    outbox: NotificationOutbox,
    registry: DeviceRegistry,
) -> None:
    await registry.upsert(device(), principal())
    surface_id = UUID(int=DEVICE_ID.int + 1)
    surface_values = device(
        device_id=surface_id,
        client_device_id="surface-device-a",
        token=None,
    ).model_dump()
    surface_values.update(
        {
            "kind": DeviceKind.SURFACE,
            "platform": "telegram",
            "app_bundle_id": None,
            "push_provider": PushProvider.TELEGRAM,
            "push_token": "paired-chat-reference",
        }
    )
    await registry.upsert(Device.model_validate(surface_values), principal())
    assert (
        await outbox.enqueue(
            new_notification(dedupe_key=f"device.test:{surface_id}:provider-partition")
        )
        is not None
    )

    assert (
        await outbox.claim_due(
            NOW,
            1,
            "notify-a",
            30,
            frozenset({PushProvider.APNS}),
        )
        == []
    )
    [claimed] = await outbox.claim_due(
        NOW,
        1,
        "surface-a",
        30,
        frozenset({PushProvider.TELEGRAM}),
    )
    assert claimed.attempts == 1
    assert claimed.claimed_by == "surface-a"


async def assert_notification_delivery_attempt_is_unique(
    outbox: NotificationOutbox,
    registry: DeviceRegistry,
) -> None:
    await registry.upsert(device(token=None), principal())
    notification = await outbox.enqueue(new_notification())
    assert notification is not None
    stranger = Principal(
        tenant_id=TENANT,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )
    stranger_device = device(
        device_id=UUID(int=DEVICE_ID.int + 20),
        client_device_id="principal-b-device",
        principal_id=stranger.principal_id,
        token=None,
    )
    await registry.upsert(stranger_device, stranger)
    with pytest.raises(NotFoundError):
        await outbox.record_delivery(
            NotificationDelivery(
                id=UUID(int=NOTIFICATION_ID.int + 99),
                notification_id=notification.id,
                device_id=stranger_device.id,
                attempt=1,
                outcome=DeliveryOutcome.DELIVERED,
                attempted_at=NOW,
            )
        )
    delivery = NotificationDelivery(
        id=UUID(int=NOTIFICATION_ID.int + 100),
        notification_id=notification.id,
        device_id=DEVICE_ID,
        attempt=1,
        outcome=DeliveryOutcome.DELIVERED,
        provider_id="provider-1",
        attempted_at=NOW,
    )
    await outbox.record_delivery(delivery)
    assert await outbox.list_deliveries(notification.id) == [delivery]
    second = await outbox.enqueue(
        new_notification(
            notification_id=UUID(int=NOTIFICATION_ID.int + 1),
            dedupe_key="test:delivery-batch-second",
        )
    )
    assert second is not None
    assert await outbox.list_deliveries_for((notification.id, second.id)) == {
        notification.id: [delivery],
        second.id: [],
    }
    with pytest.raises(ConflictError):
        await outbox.record_delivery(
            delivery.model_copy(update={"id": UUID(int=delivery.id.int + 1)})
        )


async def test_notification_enqueue_deduplicates_and_lists_by_principal() -> None:
    registry = InMemoryDeviceRegistry()
    await assert_notification_enqueue_deduplicates_and_lists_by_principal(
        InMemoryNotificationOutbox(FixedClock(NOW), registry)
    )


async def test_notification_claim_settle_and_pagination() -> None:
    registry = InMemoryDeviceRegistry()
    await assert_notification_claim_settle_and_pagination(
        InMemoryNotificationOutbox(FixedClock(NOW), registry)
    )


async def test_notification_delivery_attempt_is_unique() -> None:
    registry = InMemoryDeviceRegistry()
    await assert_notification_delivery_attempt_is_unique(
        InMemoryNotificationOutbox(FixedClock(NOW), registry), registry
    )


async def test_notification_claim_is_partitioned_by_provider() -> None:
    registry = InMemoryDeviceRegistry()
    await assert_notification_claim_is_partitioned_by_provider(
        InMemoryNotificationOutbox(FixedClock(NOW), registry), registry
    )
