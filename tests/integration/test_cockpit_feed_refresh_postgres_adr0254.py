"""PostgreSQL proof for ADR-0254: interval feed schedules and Cockpit refreshes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.bootstrap import Composition, build
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    IntervalCadence,
    OccurrenceDisposition,
    OccurrenceTrigger,
    Schedule,
    ScheduleRefreshOutcome,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import MANUAL_RUNS_PER_DAY, ScheduleMaterializer
from tests.contract.support import agent
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 26, 16, 7, 30, tzinfo=UTC)
CADENCE = IntervalCadence(every_minutes=15, timezone="America/Los_Angeles")


async def _create(composition: Composition, schedule_id: UUID) -> Schedule:
    principal = composition.principal
    schedule = Schedule(
        id=schedule_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=datetime(2026, 10, 26, 16, 15, tzinfo=UTC),
        created_at=NOW,
        updated_at=NOW,
    )
    revision = ScheduleRevision(
        schedule_id=schedule_id,
        revision=1,
        title="Pull-request feed",
        instruction="Publish the open pull requests.",
        agent_id=agent().id,
        agent_version=agent().version,
        policy_profile=agent().policy_profile,
        requested_scopes=frozenset({"cockpit.read", "cockpit.write"}),
        limits=RunLimits(max_steps=4, max_model_calls=4, max_tool_calls=4, max_cost=Decimal("1")),
        run_timeout_seconds=60,
        cadence=CADENCE,
        timezone=CADENCE.timezone,
        misfire_grace_seconds=60,
        max_consecutive_failures=3,
        created_by_principal_id=principal.principal_id,
        created_at=NOW,
    )
    async with composition.uow_factory() as uow:
        await uow.agents.put(agent())
        await uow.schedules.create(schedule, revision)
    return schedule


async def _complete(composition: Composition, run_id: UUID | None) -> None:
    assert run_id is not None
    async with composition.uow_factory() as uow:
        await uow.runs.transition(run_id, RunStatus.QUEUED, RunStatus.RUNNING)
        await uow.runs.transition(run_id, RunStatus.RUNNING, RunStatus.COMPLETED)


async def test_postgres_interval_feeds_refresh_without_spending_run_now() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        principal = composition.principal
        schedule_id = uuid4()
        await _create(composition, schedule_id)
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(
                principal.model_copy(
                    update={"scopes": {*principal.scopes, "cockpit.read", "cockpit.write"}}
                )
            ),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        clock = composition.clock
        assert isinstance(clock, FixedClock)

        async with composition.uow_factory() as uow:
            stored = await uow.schedules.get_revision(schedule_id, 1, principal)
        assert stored.cadence == CADENCE

        for _ in range(MANUAL_RUNS_PER_DAY + 1):
            refreshes = await materializer.refresh_feeds(principal)
            assert [(item.schedule_id, item.outcome) for item in refreshes] == [
                (schedule_id, ScheduleRefreshOutcome.STARTED)
            ]
            async with composition.uow_factory() as uow:
                latest = await uow.schedule_occurrences.latest_materialized(schedule_id)
            assert latest is not None and latest.trigger is OccurrenceTrigger.MANUAL
            assert latest.manual_request_key is not None
            assert latest.manual_request_key.startswith("cockpit.refresh:")
            await _complete(composition, latest.run_id)
            clock.advance(timedelta(minutes=1))

        async with composition.uow_factory() as uow:
            counted = await uow.schedule_occurrences.count_manual_since(
                schedule_id, NOW - timedelta(days=1)
            )
        assert counted == 0
        owner_run, replayed = await materializer.materialize_manual(
            principal, schedule_id, expected_revision=1, request_key="owner-asked"
        )
        assert not replayed
        await _complete(composition, owner_run.run_id)

        clock.advance(datetime(2026, 10, 26, 16, 15, tzinfo=UTC) - clock.now())
        fired = await materializer.materialize(schedule_id)
        assert fired is not None
        assert fired.trigger is OccurrenceTrigger.SCHEDULED
        assert fired.disposition is OccurrenceDisposition.MATERIALIZED
        async with composition.uow_factory() as uow:
            current = await uow.schedules.get(schedule_id, principal)
        assert current.next_fire_at == datetime(2026, 10, 26, 16, 30, tzinfo=UTC)
