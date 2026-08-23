"""Shared device-registration request-idempotency repository contract."""

import pytest

from agent_core.adapters.persistence.notifications import (
    InMemoryDeviceRegistrationIdempotencyRepository,
)
from agent_core.domain.devices import DeviceRegistrationIdempotencyRecord
from agent_core.domain.errors import ConflictError
from agent_core.ports.devices import DeviceRegistrationIdempotencyRepository
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT


async def assert_device_registration_idempotency_replays_exact_response_and_rejects_reuse(
    repository: DeviceRegistrationIdempotencyRepository,
) -> None:
    record = DeviceRegistrationIdempotencyRecord(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        key="device-request-1",
        request_hash="a" * 64,
        response={"id": "00000000-0000-0000-0000-000000000212", "status": "active"},
        created_at=NOW,
    )

    assert await repository.create(record) == record
    assert await repository.create(record) == record
    assert await repository.get(TENANT, PRINCIPAL_ID, record.key) == record

    with pytest.raises(ConflictError):
        await repository.create(record.model_copy(update={"request_hash": "b" * 64}))


async def test_in_memory_device_registration_idempotency_repository_satisfies_contract() -> None:
    await assert_device_registration_idempotency_replays_exact_response_and_rejects_reuse(
        InMemoryDeviceRegistrationIdempotencyRepository()
    )
