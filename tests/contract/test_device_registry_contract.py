"""Shared device-registry contract."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from agent_core.adapters.persistence.notifications import InMemoryDeviceRegistry
from agent_core.domain.agents import Principal
from agent_core.domain.devices import (
    Device,
    DeviceCursor,
    DeviceKind,
    DeviceStatus,
    PushEnvironment,
    PushProvider,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.notifications import NotificationKind
from agent_core.ports.devices import DeviceRegistry
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT, principal

DEVICE_ID = UUID("00000000-0000-0000-0000-000000000212")


def device(
    *,
    device_id: UUID = DEVICE_ID,
    client_device_id: str = "client-device-a",
    principal_id: str = PRINCIPAL_ID,
    token: str | None = "push-token-a",
    muted_kinds: frozenset[NotificationKind] = frozenset(),
    created_at: datetime = NOW,
) -> Device:
    return Device(
        id=device_id,
        tenant_id=TENANT,
        principal_id=principal_id,
        client_device_id=client_device_id,
        name="Contract phone",
        kind=DeviceKind.MOBILE,
        platform="ios",
        app_bundle_id="com.example.veetbot",
        push_provider=None if token is None else PushProvider.APNS,
        push_token=None if token is None else SecretStr(token),
        push_environment=None if token is None else PushEnvironment.SANDBOX,
        push_token_updated_at=None if token is None else created_at,
        muted_kinds=muted_kinds,
        status=DeviceStatus.ACTIVE,
        last_seen_at=created_at,
        created_at=created_at,
        updated_at=created_at,
    )


async def assert_device_registration_is_idempotent_and_principal_scoped(
    registry: DeviceRegistry,
) -> None:
    original = device()
    assert await registry.upsert(original, principal()) == original

    refreshed_input = device(device_id=UUID(int=DEVICE_ID.int + 1)).model_copy(
        update={"name": "Renamed phone", "updated_at": NOW + timedelta(seconds=1)}
    )
    refreshed = await registry.upsert(refreshed_input, principal())
    assert refreshed.id == original.id
    assert refreshed.created_at == original.created_at
    assert refreshed.name == "Renamed phone"
    assert await registry.get_by_client_device_id(original.client_device_id, principal()) == (
        refreshed
    )
    assert await registry.list(principal(), limit=10) == [refreshed]

    stranger = Principal(
        tenant_id=TENANT,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )
    stranger_device = device(
        device_id=UUID(int=DEVICE_ID.int + 2),
        principal_id=stranger.principal_id,
        token="push-token-b",
    )
    assert await registry.upsert(stranger_device, stranger) == stranger_device
    with pytest.raises(NotFoundError):
        await registry.get(original.id, stranger)
    assert await registry.get_by_client_device_id(original.client_device_id, stranger) == (
        stranger_device
    )
    with pytest.raises(NotFoundError):
        await registry.upsert(original, stranger)


async def assert_device_tokens_move_and_lifecycle_removes_targets(
    registry: DeviceRegistry,
) -> None:
    first = device()
    second = device(
        device_id=UUID(int=DEVICE_ID.int + 1),
        client_device_id="client-device-b",
    ).model_copy(
        update={
            "created_at": NOW + timedelta(seconds=1),
            "updated_at": NOW + timedelta(seconds=1),
            "last_seen_at": NOW + timedelta(seconds=1),
            "push_token_updated_at": NOW + timedelta(seconds=1),
        }
    )
    await registry.upsert(first, principal())
    await registry.upsert(second, principal())

    moved_from = await registry.get(first.id, principal())
    assert moved_from.push_token is None
    assert moved_from.push_provider is None
    targets = await registry.push_targets(TENANT, PRINCIPAL_ID, NotificationKind.RUN_FAILED)
    assert [target.device_id for target in targets] == [second.id]

    stranger = Principal(
        tenant_id=TENANT,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )
    conflicting = device(
        device_id=UUID(int=DEVICE_ID.int + 20),
        client_device_id="client-device-foreign",
        principal_id=stranger.principal_id,
    )
    with pytest.raises(ConflictError) as conflict:
        await registry.upsert(conflicting, stranger)
    assert "push-token-a" not in str(conflict.value)

    revoked = await registry.revoke(second.id, principal(), NOW + timedelta(seconds=2))
    assert revoked.status is DeviceStatus.REVOKED
    assert revoked.push_token is None
    assert await registry.push_targets(TENANT, PRINCIPAL_ID, NotificationKind.RUN_FAILED) == []

    third = device(
        device_id=UUID(int=DEVICE_ID.int + 2),
        client_device_id="client-device-c",
        token="push-token-c",
        muted_kinds=frozenset({NotificationKind.RUN_FAILED}),
    )
    await registry.upsert(third, principal())
    assert await registry.push_targets(TENANT, PRINCIPAL_ID, NotificationKind.RUN_FAILED) == []
    invalidated = await registry.invalidate_push_token(
        third.id, "BadDeviceToken", NOW + timedelta(seconds=3)
    )
    assert invalidated is not None and invalidated.push_token is None
    assert (
        await registry.invalidate_push_token(third.id, "BadDeviceToken", NOW + timedelta(seconds=4))
        is None
    )
    await registry.delete(third.id, principal())
    with pytest.raises(NotFoundError):
        await registry.get(third.id, principal())


async def assert_device_listing_is_stable(registry: DeviceRegistry) -> None:
    values = [
        device(
            device_id=UUID(int=DEVICE_ID.int + offset),
            client_device_id=f"client-device-{offset}",
            token=f"push-token-{offset}",
            created_at=NOW + timedelta(seconds=offset),
        )
        for offset in range(3)
    ]
    for value in values:
        await registry.upsert(value, principal())
    first_page = await registry.list(principal(), limit=2)
    assert [value.id for value in first_page] == [values[2].id, values[1].id]
    cursor = DeviceCursor(created_at=first_page[-1].created_at, id=first_page[-1].id)
    assert await registry.list(principal(), limit=2, cursor=cursor) == [values[0]]


async def test_device_registration_is_idempotent_and_principal_scoped() -> None:
    await assert_device_registration_is_idempotent_and_principal_scoped(InMemoryDeviceRegistry())


async def test_device_tokens_move_and_lifecycle_removes_targets() -> None:
    await assert_device_tokens_move_and_lifecycle_removes_targets(InMemoryDeviceRegistry())


async def test_device_listing_is_stable() -> None:
    await assert_device_listing_is_stable(InMemoryDeviceRegistry())
