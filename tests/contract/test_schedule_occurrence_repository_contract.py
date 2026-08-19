"""Shared schedule-occurrence repository contract."""

from uuid import UUID

import pytest

from agent_core.adapters.persistence.schedules import (
    InMemoryScheduleOccurrenceRepository,
    InMemoryScheduleRepository,
)
from agent_core.domain.errors import ConflictError
from agent_core.domain.schedules import OccurrenceDisposition, ScheduleOccurrence
from agent_core.ports.schedules import ScheduleOccurrenceRepository
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal
from tests.contract.test_schedule_repository_contract import SCHEDULE_ID, revision, schedule


async def assert_occurrence_insert_is_idempotent_by_schedule_and_nominal_instant(
    repository: ScheduleOccurrenceRepository,
) -> None:
    occurrence = ScheduleOccurrence(
        id=UUID(int=300),
        schedule_id=SCHEDULE_ID,
        schedule_revision=1,
        nominal_fire_at=NOW,
        disposition=OccurrenceDisposition.MISSED,
        reason_code="schedule.grace_expired",
        created_at=NOW,
    )

    assert await repository.insert(occurrence) == occurrence
    assert await repository.insert(occurrence) == occurrence
    assert await repository.get_by_nominal(SCHEDULE_ID, NOW) == occurrence
    assert await repository.latest_at_or_before(SCHEDULE_ID, NOW) == occurrence
    assert await repository.latest_materialized(SCHEDULE_ID) is None
    assert await repository.list(SCHEDULE_ID, principal(), limit=10) == [occurrence]

    with pytest.raises(ConflictError):
        await repository.insert(
            occurrence.model_copy(
                update={"id": UUID(int=301), "reason_code": "schedule.rate_limit"}
            )
        )

    materialized = ScheduleOccurrence(
        id=UUID(int=302),
        schedule_id=SCHEDULE_ID,
        schedule_revision=1,
        nominal_fire_at=NOW.replace(microsecond=NOW.microsecond + 1),
        disposition=OccurrenceDisposition.MATERIALIZED,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        authority_version="authority-1",
        materialized_at=NOW.replace(microsecond=NOW.microsecond + 1),
        created_at=NOW.replace(microsecond=NOW.microsecond + 1),
    )
    await repository.insert(materialized)
    assert await repository.latest_materialized(SCHEDULE_ID) == materialized
    link = await repository.get_by_run(RUN_ID)
    assert link is not None
    assert link.occurrence == materialized
    assert link.tenant_id == principal().tenant_id
    assert link.principal_id == principal().principal_id


async def test_in_memory_schedule_occurrence_repository_satisfies_contract() -> None:
    schedules = InMemoryScheduleRepository()
    await schedules.create(schedule(), revision())
    await assert_occurrence_insert_is_idempotent_by_schedule_and_nominal_instant(
        InMemoryScheduleOccurrenceRepository(schedules)
    )
