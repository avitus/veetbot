"""Shared schedule request-idempotency repository contract."""

import pytest

from agent_core.adapters.persistence.schedules import (
    InMemoryScheduleIdempotencyRepository,
    InMemoryScheduleRepository,
)
from agent_core.domain.errors import ConflictError
from agent_core.domain.schedules import ScheduleIdempotencyRecord
from agent_core.ports.schedules import ScheduleIdempotencyRepository
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT
from tests.contract.test_schedule_repository_contract import SCHEDULE_ID, revision, schedule


async def assert_schedule_idempotency_replays_exact_requests_and_rejects_reuse(
    repository: ScheduleIdempotencyRepository,
) -> None:
    record = ScheduleIdempotencyRecord(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        key="request-1",
        request_hash="a" * 64,
        schedule_id=SCHEDULE_ID,
        created_at=NOW,
    )

    assert await repository.create(record) == record
    assert await repository.create(record) == record
    assert await repository.get(TENANT, PRINCIPAL_ID, "request-1") == record

    with pytest.raises(ConflictError):
        await repository.create(record.model_copy(update={"request_hash": "b" * 64}))


async def test_in_memory_schedule_idempotency_repository_satisfies_contract() -> None:
    schedules = InMemoryScheduleRepository()
    await schedules.create(schedule(), revision())
    await assert_schedule_idempotency_replays_exact_requests_and_rejects_reuse(
        InMemoryScheduleIdempotencyRepository(schedules)
    )
