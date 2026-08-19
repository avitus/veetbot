"""Shared schedule-definition repository contract."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from agent_core.adapters.persistence.schedules import InMemoryScheduleRepository
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.runs import RunLimits
from agent_core.domain.schedules import (
    DailyCadence,
    Schedule,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.ports.schedules import ScheduleRepository
from tests.contract.support import AGENT_ID, NOW, PRINCIPAL_ID, TENANT, principal

SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000000211")


def schedule(
    *,
    schedule_id: UUID = SCHEDULE_ID,
    next_fire_at: datetime | None = NOW,
    updated_at: datetime = NOW,
) -> Schedule:
    return Schedule(
        id=schedule_id,
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=next_fire_at,
        created_at=NOW,
        updated_at=updated_at,
    )


def revision(schedule_id: UUID = SCHEDULE_ID) -> ScheduleRevision:
    return ScheduleRevision(
        schedule_id=schedule_id,
        revision=1,
        title="Daily briefing",
        instruction="Summarize project changes.",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=frozenset({"memory.read"}),
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("1"),
        ),
        run_timeout_seconds=60,
        cadence=DailyCadence(local_time=time(9), timezone="UTC"),
        timezone="UTC",
        misfire_grace_seconds=60,
        max_consecutive_failures=3,
        created_by_principal_id=PRINCIPAL_ID,
        created_at=NOW,
    )


async def assert_schedule_repository_is_principal_isolated_and_revisioned(
    repository: ScheduleRepository,
) -> None:
    value = schedule()
    initial_revision = revision()

    assert await repository.create(value, initial_revision) == value
    assert await repository.get(SCHEDULE_ID, principal()) == value
    assert await repository.get_revision(SCHEDULE_ID, 1, principal()) == initial_revision

    stranger = Principal(
        tenant_id=TENANT,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )
    with pytest.raises(NotFoundError):
        await repository.get(SCHEDULE_ID, stranger)
    with pytest.raises(NotFoundError):
        await repository.get_revision(SCHEDULE_ID, 1, stranger)
    with pytest.raises(ConflictError):
        await repository.create(value, initial_revision)


async def assert_schedule_repository_lists_and_finds_due_definitions_deterministically(
    repository: ScheduleRepository,
) -> None:
    later_id = UUID(int=SCHEDULE_ID.int + 1)
    future_id = UUID(int=SCHEDULE_ID.int + 2)
    await repository.create(schedule(), revision())
    await repository.create(
        schedule(schedule_id=later_id, updated_at=NOW + timedelta(seconds=1)),
        revision(later_id),
    )
    await repository.create(
        schedule(schedule_id=future_id, next_fire_at=NOW + timedelta(days=1)),
        revision(future_id),
    )

    assert [item.id for item in await repository.list(principal(), limit=2)] == [
        later_id,
        future_id,
    ]
    assert await repository.due(NOW, 10) == [SCHEDULE_ID, later_id]
    assert await repository.next_fire_at() == NOW
    assert await repository.due(NOW - timedelta(microseconds=1), 10) == []


async def assert_schedule_repository_locks_and_advances_one_due_definition(
    repository: ScheduleRepository,
) -> None:
    current = schedule()
    await repository.create(current, revision())
    assert await repository.lock_due(SCHEDULE_ID, NOW) == current
    completed = current.model_copy(
        update={
            "state": ScheduleState.COMPLETED,
            "next_fire_at": None,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    assert await repository.advance(current, completed) == completed
    assert await repository.lock_due(SCHEDULE_ID, NOW + timedelta(days=1)) is None
    assert await repository.due(NOW + timedelta(days=1), 10) == []


async def assert_schedule_repository_mutates_state_and_revisions_with_cas(
    repository: ScheduleRepository,
) -> None:
    current = schedule()
    await repository.create(current, revision())
    second_revision = revision().model_copy(
        update={"revision": 2, "instruction": "Use the second immutable revision."}
    )
    updated = current.model_copy(
        update={
            "current_revision": 2,
            "next_fire_at": NOW + timedelta(days=1),
            "updated_at": NOW + timedelta(seconds=1),
        }
    )

    assert await repository.replace(current, updated, second_revision) == updated
    assert await repository.get_revision(SCHEDULE_ID, 1, principal()) == revision()
    assert await repository.get_revision(SCHEDULE_ID, 2, principal()) == second_revision
    with pytest.raises(ConflictError):
        await repository.replace(current, updated, second_revision)


async def test_schedule_repository_is_principal_isolated_and_revisioned() -> None:
    await assert_schedule_repository_is_principal_isolated_and_revisioned(
        InMemoryScheduleRepository()
    )


async def test_schedule_repository_lists_and_finds_due_definitions_deterministically() -> None:
    await assert_schedule_repository_lists_and_finds_due_definitions_deterministically(
        InMemoryScheduleRepository()
    )


async def test_schedule_repository_locks_and_advances_one_due_definition() -> None:
    await assert_schedule_repository_locks_and_advances_one_due_definition(
        InMemoryScheduleRepository()
    )


async def test_schedule_repository_mutates_state_and_revisions_with_cas() -> None:
    await assert_schedule_repository_mutates_state_and_revisions_with_cas(
        InMemoryScheduleRepository()
    )
