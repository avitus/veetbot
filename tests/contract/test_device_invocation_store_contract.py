"""Shared device-invocation store contract."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.persistence.device_channel import InMemoryDeviceInvocationStore
from agent_core.domain.devices import DeviceInvocation, DeviceInvocationStatus
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.device_channel import DeviceInvocationStore
from tests.contract.support import NOW, RUN_ID, TENANT

DEVICE_ID = UUID("00000000-0000-0000-0000-000000000260")
OTHER_DEVICE_ID = UUID("00000000-0000-0000-0000-000000000261")
INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000262")
SECOND_INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000263")
FOREIGN_INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000264")
UNKNOWN_INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000265")


def invocation(**updates: Any) -> DeviceInvocation:
    values: dict[str, Any] = {
        "id": INVOCATION_ID,
        "tenant_id": TENANT,
        "device_id": DEVICE_ID,
        "run_id": RUN_ID,
        "tool_name": "device.sms.send",
        "arguments": {"recipient": "contract-recipient", "body": "contract body"},
        "status": DeviceInvocationStatus.PENDING,
        "created_at": NOW,
    }
    values.update(updates)
    return DeviceInvocation.model_validate(values)


async def assert_invocation_creation_is_idempotent_and_device_scoped(
    store: DeviceInvocationStore,
) -> None:
    created = await store.create(invocation())

    assert created == invocation()
    assert await store.create(invocation(tool_name="device.other.send")) is None
    assert await store.get(INVOCATION_ID) == invocation()
    with pytest.raises(NotFoundError):
        await store.get(UNKNOWN_INVOCATION_ID)

    later = invocation(id=SECOND_INVOCATION_ID, created_at=NOW + timedelta(seconds=5))
    assert await store.create(later) == later
    foreign = invocation(id=FOREIGN_INVOCATION_ID, device_id=OTHER_DEVICE_ID)
    assert await store.create(foreign) == foreign

    horizon = NOW + timedelta(seconds=10)
    assert [value.id for value in await store.list_pending_for_device(DEVICE_ID, now=horizon)] == [
        INVOCATION_ID,
        SECOND_INVOCATION_ID,
    ]
    assert [value.id for value in await store.list_pending_for_device(DEVICE_ID, now=NOW)] == [
        INVOCATION_ID
    ]
    assert [
        value.id for value in await store.list_pending_for_device(OTHER_DEVICE_ID, now=horizon)
    ] == [FOREIGN_INVOCATION_ID]


async def assert_first_result_wins_and_expired_rows_refuse_results(
    store: DeviceInvocationStore,
) -> None:
    await store.create(invocation())

    with pytest.raises(NotFoundError):
        await store.record_result(
            INVOCATION_ID,
            device_id=OTHER_DEVICE_ID,
            status=DeviceInvocationStatus.SENT,
            at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(NotFoundError):
        await store.record_result(
            UNKNOWN_INVOCATION_ID,
            device_id=DEVICE_ID,
            status=DeviceInvocationStatus.SENT,
            at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="terminal"):
        await store.record_result(
            INVOCATION_ID,
            device_id=DEVICE_ID,
            status=DeviceInvocationStatus.PENDING,
            at=NOW + timedelta(seconds=1),
        )

    sent = await store.record_result(
        INVOCATION_ID,
        device_id=DEVICE_ID,
        status=DeviceInvocationStatus.SENT,
        at=NOW + timedelta(seconds=1),
    )
    assert sent.status is DeviceInvocationStatus.SENT
    assert sent.resolved_at == NOW + timedelta(seconds=1)
    assert await store.list_pending_for_device(DEVICE_ID, now=NOW + timedelta(seconds=10)) == []

    replayed = await store.record_result(
        INVOCATION_ID,
        device_id=DEVICE_ID,
        status=DeviceInvocationStatus.SENT,
        at=NOW + timedelta(seconds=2),
    )
    assert replayed == sent
    mismatched = await store.record_result(
        INVOCATION_ID,
        device_id=DEVICE_ID,
        status=DeviceInvocationStatus.FAILED,
        at=NOW + timedelta(seconds=3),
    )
    assert mismatched == sent
    assert await store.get(INVOCATION_ID) == sent

    await store.create(invocation(id=SECOND_INVOCATION_ID))
    expired = await store.record_result(
        SECOND_INVOCATION_ID,
        device_id=DEVICE_ID,
        status=DeviceInvocationStatus.EXPIRED,
        at=NOW + timedelta(seconds=4),
    )
    assert expired.status is DeviceInvocationStatus.EXPIRED
    for posted in (DeviceInvocationStatus.EXPIRED, DeviceInvocationStatus.SENT):
        with pytest.raises(ConflictError):
            await store.record_result(
                SECOND_INVOCATION_ID,
                device_id=DEVICE_ID,
                status=posted,
                at=NOW + timedelta(seconds=5),
            )
    assert await store.get(SECOND_INVOCATION_ID) == expired


async def assert_expiry_sweeps_only_overdue_pending_rows(
    store: DeviceInvocationStore,
) -> None:
    await store.create(invocation())
    await store.create(invocation(id=SECOND_INVOCATION_ID, created_at=NOW + timedelta(seconds=200)))
    await store.create(invocation(id=FOREIGN_INVOCATION_ID, device_id=OTHER_DEVICE_ID))
    await store.record_result(
        FOREIGN_INVOCATION_ID,
        device_id=OTHER_DEVICE_ID,
        status=DeviceInvocationStatus.SENT,
        at=NOW + timedelta(seconds=1),
    )

    swept_at = NOW + timedelta(seconds=300)
    assert await store.expire_overdue(now=swept_at, timeout_seconds=300) == 1

    overdue = await store.get(INVOCATION_ID)
    assert overdue.status is DeviceInvocationStatus.EXPIRED
    assert overdue.resolved_at == swept_at
    assert (await store.get(SECOND_INVOCATION_ID)).status is DeviceInvocationStatus.PENDING
    assert (await store.get(FOREIGN_INVOCATION_ID)).status is DeviceInvocationStatus.SENT
    assert await store.expire_overdue(now=swept_at, timeout_seconds=300) == 0

    with pytest.raises(ConflictError):
        await store.record_result(
            INVOCATION_ID,
            device_id=DEVICE_ID,
            status=DeviceInvocationStatus.SENT,
            at=swept_at + timedelta(seconds=1),
        )


async def test_in_memory_invocation_creation_is_idempotent_and_device_scoped() -> None:
    await assert_invocation_creation_is_idempotent_and_device_scoped(
        InMemoryDeviceInvocationStore()
    )


async def test_in_memory_first_result_wins_and_expired_rows_refuse_results() -> None:
    await assert_first_result_wins_and_expired_rows_refuse_results(InMemoryDeviceInvocationStore())


async def test_in_memory_expiry_sweeps_only_overdue_pending_rows() -> None:
    await assert_expiry_sweeps_only_overdue_pending_rows(InMemoryDeviceInvocationStore())
