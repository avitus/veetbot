"""PostgreSQL proof for ADR-0215 part C: manual Run-now occurrences."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.bootstrap import Composition, build
from agent_core.domain.errors import ConflictError, RateLimitedError
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    DailyCadence,
    OccurrenceDisposition,
    OccurrenceTrigger,
    Schedule,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import MANUAL_RUNS_PER_DAY, ScheduleMaterializer
from tests.contract.support import agent
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 20, 15, tzinfo=UTC)


async def _create(composition: Composition, schedule_id: UUID) -> Schedule:
    principal = composition.principal
    schedule = Schedule(
        id=schedule_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=NOW + timedelta(hours=18),
        created_at=NOW,
        updated_at=NOW,
    )
    revision = ScheduleRevision(
        schedule_id=schedule_id,
        revision=1,
        title="Run-now proof",
        instruction="Prove a manual occurrence commits atomically.",
        agent_id=agent().id,
        agent_version=agent().version,
        policy_profile=agent().policy_profile,
        requested_scopes=frozenset({"session.read"}),
        limits=RunLimits(max_steps=4, max_model_calls=4, max_tool_calls=4, max_cost=Decimal("1")),
        run_timeout_seconds=60,
        cadence=DailyCadence(local_time=time(9), timezone="UTC"),
        timezone="UTC",
        misfire_grace_seconds=60,
        max_consecutive_failures=3,
        created_by_principal_id=principal.principal_id,
        created_at=NOW,
    )
    async with composition.uow_factory() as uow:
        await uow.agents.put(agent())
        await uow.schedules.create(schedule, revision)
    return schedule


def _materializer(composition: Composition) -> ScheduleMaterializer:
    return ScheduleMaterializer(
        uow_factory=composition.uow_factory,
        principals=StaticSchedulePrincipalDirectory(composition.principal),
        admission=AllowScheduleAdmissionController(),
        clock=composition.clock,
        ids=composition.ids,
        seed_checkpoint=DurableCheckpointSeeder(composition.clock),
    )


async def test_postgres_manual_runs_replay_by_key_count_per_day_and_share_instants() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        schedule_id = uuid4()
        schedule = await _create(composition, schedule_id)
        materializer = _materializer(composition)
        principal = composition.principal

        first, replayed = await materializer.materialize_manual(
            principal, schedule_id, expected_revision=1, request_key="manual-0"
        )
        assert not replayed
        assert first.trigger is OccurrenceTrigger.MANUAL
        assert first.disposition is OccurrenceDisposition.MATERIALIZED
        again, replayed = await materializer.materialize_manual(
            principal, schedule_id, expected_revision=1, request_key="manual-0"
        )
        assert replayed and again == first
        with pytest.raises(ConflictError) as busy:
            await materializer.materialize_manual(
                principal, schedule_id, expected_revision=1, request_key="manual-1"
            )
        assert busy.value.reason == "schedule.run_active"

        # Several manual runs at one instant are distinct rows; only a cadence
        # instant is unique per schedule.
        running = first
        for index in range(1, MANUAL_RUNS_PER_DAY + 1):
            assert running.run_id is not None
            async with composition.uow_factory() as uow:
                await uow.runs.transition(running.run_id, RunStatus.QUEUED, RunStatus.RUNNING)
                await uow.runs.transition(running.run_id, RunStatus.RUNNING, RunStatus.COMPLETED)
            if index == MANUAL_RUNS_PER_DAY:
                break
            running, _ = await materializer.materialize_manual(
                principal, schedule_id, expected_revision=1, request_key=f"manual-{index}"
            )
            assert running.nominal_fire_at == NOW
        async with composition.uow_factory() as uow:
            assert (
                await uow.schedule_occurrences.count_manual_since(
                    schedule_id, NOW - timedelta(days=1)
                )
                == MANUAL_RUNS_PER_DAY
            )
            current = await uow.schedules.get(schedule_id, principal)
        assert current.next_fire_at == schedule.next_fire_at
        with pytest.raises(RateLimitedError):
            await materializer.materialize_manual(
                principal, schedule_id, expected_revision=1, request_key="manual-extra"
            )

        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(timedelta(hours=18))
        scheduled = await materializer.materialize(schedule_id)
        assert scheduled is not None
        assert scheduled.trigger is OccurrenceTrigger.SCHEDULED
        assert scheduled.disposition is OccurrenceDisposition.MATERIALIZED
