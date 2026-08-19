"""PostgreSQL proof for Milestone 11 schedule materialization."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.bootstrap import Composition, build
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    DailyCadence,
    OccurrenceDisposition,
    OnceCadence,
    Schedule,
    ScheduleOccurrence,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import ScheduleMaterializer
from tests.contract.support import agent
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)
WRITE_BOUNDARIES = (
    "occurrence",
    "session",
    "session_event",
    "run",
    "instruction",
    "queued_event",
    "checkpoint",
    "process_event",
    "schedule",
)


class _InjectedCrashError(Exception):
    pass


def _schedule(composition: Composition, schedule_id: UUID) -> tuple[Schedule, ScheduleRevision]:
    principal = composition.principal
    return (
        Schedule(
            id=schedule_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            state=ScheduleState.ACTIVE,
            current_revision=1,
            next_fire_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        ),
        ScheduleRevision(
            schedule_id=schedule_id,
            revision=1,
            title="Scheduled transaction proof",
            instruction="Prove that this instruction is committed atomically.",
            agent_id=agent().id,
            agent_version=agent().version,
            policy_profile=agent().policy_profile,
            requested_scopes=frozenset({"session.read"}),
            limits=RunLimits(
                max_steps=4,
                max_model_calls=4,
                max_tool_calls=4,
                max_cost=Decimal("1"),
            ),
            run_timeout_seconds=60,
            cadence=OnceCadence(at=NOW),
            timezone=None,
            misfire_grace_seconds=60,
            max_consecutive_failures=3,
            created_by_principal_id=principal.principal_id,
            created_at=NOW,
        ),
    )


def _materializer(
    composition: Composition,
    *,
    crash_at: str | None = None,
    allow_all_admission: bool = False,
) -> ScheduleMaterializer:
    def probe(boundary: str) -> None:
        if boundary == crash_at:
            raise _InjectedCrashError(boundary)

    return ScheduleMaterializer(
        uow_factory=composition.uow_factory,
        principals=StaticSchedulePrincipalDirectory(composition.principal),
        admission=(AllowScheduleAdmissionController() if allow_all_admission else None),
        clock=composition.clock,
        ids=composition.ids,
        seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        write_probe=probe,
    )


async def _create_due_schedule(composition: Composition, schedule_id: UUID) -> None:
    schedule, revision = _schedule(composition, schedule_id)
    async with composition.uow_factory() as uow:
        await uow.agents.put(agent())
        await uow.schedules.create(schedule, revision)


async def _assert_complete(
    composition: Composition, schedule_id: UUID, occurrence: ScheduleOccurrence
) -> None:
    assert occurrence.disposition is OccurrenceDisposition.MATERIALIZED
    assert occurrence.session_id is not None
    assert occurrence.run_id is not None
    async with composition.uow_factory() as uow:
        assert await uow.schedule_occurrences.list(
            schedule_id, composition.principal, limit=10
        ) == [occurrence]
        session = await uow.sessions.get(occurrence.session_id, composition.principal)
        run = await uow.runs.get(occurrence.run_id, composition.principal)
        assert session.metadata["schedule_occurrence_id"] == str(occurrence.id)
        assert run.status is RunStatus.QUEUED
        assert run.priority == 10
        assert run.principal_scopes == {"session.read"}
        assert await uow.checkpoints.latest(run.id) is not None
        events = await uow.events.list_after(session.id, 0, composition.principal)
        assert [event.event_type for event in events] == [
            "session.created",
            "user.message.created",
            "run.queued",
            "run.checkpointed",
        ]
        process_events = await uow.process_events.list("schedule.occurrence.materialized")
        [process_event] = [
            event for event in process_events if event.payload["schedule_id"] == str(schedule_id)
        ]
        assert "instruction" not in process_event.payload


async def test_concurrent_schedulers_materialize_once() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        schedule_id = uuid4()
        await _create_due_schedule(composition, schedule_id)

        first, second = await asyncio.gather(
            _materializer(composition).materialize(schedule_id),
            _materializer(composition).materialize(schedule_id),
        )

        occurrence = first or second
        assert occurrence is not None
        await _assert_complete(composition, schedule_id, occurrence)


async def test_materialization_rolls_back_at_every_write_boundary_and_retries() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        for boundary in WRITE_BOUNDARIES:
            schedule_id = uuid4()
            await _create_due_schedule(composition, schedule_id)

            with pytest.raises(_InjectedCrashError, match=boundary):
                await _materializer(
                    composition, crash_at=boundary, allow_all_admission=True
                ).materialize(schedule_id)

            async with composition.uow_factory() as uow:
                schedule = await uow.schedules.get(schedule_id, composition.principal)
                assert schedule.state is ScheduleState.ACTIVE
                assert schedule.next_fire_at == NOW
                assert (
                    await uow.schedule_occurrences.list(
                        schedule_id, composition.principal, limit=10
                    )
                    == []
                )

            occurrence = await _materializer(composition, allow_all_admission=True).materialize(
                schedule_id
            )
            assert occurrence is not None
            await _assert_complete(composition, schedule_id, occurrence)

            replay = await _materializer(composition, allow_all_admission=True).materialize(
                schedule_id
            )
            assert replay == occurrence


async def test_nonterminal_occurrence_skips_overlap_without_blocking_other_schedule() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        schedule_id, other_schedule_id = uuid4(), uuid4()
        cadence = DailyCadence(local_time=time(16), timezone="UTC")
        first_schedule, first_revision = _schedule(composition, schedule_id)
        other_schedule, other_revision = _schedule(composition, other_schedule_id)
        first_revision = first_revision.model_copy(update={"cadence": cadence, "timezone": "UTC"})
        other_schedule = other_schedule.model_copy(update={"next_fire_at": NOW + timedelta(days=1)})
        other_revision = other_revision.model_copy(update={"cadence": cadence, "timezone": "UTC"})
        async with composition.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.schedules.create(first_schedule, first_revision)
            await uow.schedules.create(other_schedule, other_revision)

        first = await _materializer(composition).materialize(schedule_id)
        assert first is not None and first.run_id is not None
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(timedelta(days=1))

        skipped = await _materializer(composition).materialize(schedule_id)
        other = await _materializer(composition).materialize(other_schedule_id)

        assert skipped is not None
        assert skipped.disposition is OccurrenceDisposition.SKIPPED_OVERLAP
        assert skipped.reason_code == "schedule.in_flight"
        assert skipped.run_id is None
        assert other is not None
        assert other.disposition is OccurrenceDisposition.MATERIALIZED
        async with composition.uow_factory() as uow:
            original_run = await uow.runs.get(first.run_id, composition.principal)
            assert original_run.status is RunStatus.QUEUED
            schedule = await uow.schedules.get(schedule_id, composition.principal)
            assert schedule.consecutive_failures == 0
            overlap_events = await uow.process_events.list("schedule.occurrence.skipped_overlap")
            assert any(
                event.payload["occurrence_id"] == str(skipped.id) for event in overlap_events
            )
