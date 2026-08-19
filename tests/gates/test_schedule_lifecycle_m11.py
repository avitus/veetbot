"""Milestone 11 schedule lifecycle and immutable-revision gates."""

import asyncio
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.application.schedule_service import ScheduleService
from agent_core.bootstrap import build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.errors import ConflictError, ScheduleValidationError
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    DailyCadence,
    ScheduleDefinition,
    ScheduleDefinitionLimits,
    ScheduleState,
)
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import ScheduleMaterializer
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)
AGENT_ID = UUID("00000000-0000-0000-0000-000000000511")


@given(
    operations=st.lists(
        st.sampled_from(("advance", "scan", "pause", "resume", "cancel")),
        min_size=1,
        max_size=100,
    )
)
@hypothesis_settings(max_examples=20, deadline=None)
def test_generated_lifecycle_histories_never_reopen_or_backfill(
    operations: list[str],
) -> None:
    async def exercise_production_lifecycle() -> None:
        principal = _principal()
        async with build(
            settings=memory_settings(),
            storage="memory",
            fixed_clock_at=NOW,
            sequential_ids=True,
            principal=principal,
        ) as composition:
            async with composition.uow_factory() as uow:
                await uow.agents.put(_agent())
            service = ScheduleService(
                uow_factory=composition.uow_factory,
                clock=composition.clock,
                ids=composition.ids,
                limits=_limits(),
            )
            materializer = ScheduleMaterializer(
                uow_factory=composition.uow_factory,
                principals=StaticSchedulePrincipalDirectory(principal),
                admission=AllowScheduleAdmissionController(),
                clock=composition.clock,
                ids=composition.ids,
                seed_checkpoint=DurableCheckpointSeeder(composition.clock),
            )
            created = await service.create(principal, _definition(), "generated-lifecycle")
            schedule_id = created.schedule.id
            terminal_seen = False
            clock = composition.clock
            assert isinstance(clock, FixedClock)

            for operation in operations:
                current = (await service.get(principal, schedule_id)).schedule
                if operation == "advance":
                    clock.advance(timedelta(days=1))
                elif operation == "scan":
                    occurrence = await materializer.materialize(schedule_id)
                    if occurrence is not None and occurrence.run_id is not None:
                        async with composition.uow_factory() as uow:
                            linked = await uow.runs.get(occurrence.run_id, principal)
                            if linked.status is RunStatus.QUEUED:
                                await uow.runs.transition(
                                    linked.id, RunStatus.QUEUED, RunStatus.RUNNING
                                )
                                await uow.runs.transition(
                                    linked.id, RunStatus.RUNNING, RunStatus.COMPLETED
                                )
                elif operation == "pause" and current.state is ScheduleState.ACTIVE:
                    await service.pause(principal, schedule_id, current.current_revision)
                elif operation == "resume" and current.state is ScheduleState.PAUSED:
                    await service.resume(principal, schedule_id, current.current_revision)
                elif operation == "cancel" and current.state is not ScheduleState.CANCELLED:
                    await service.cancel(principal, schedule_id, current.current_revision)

                persisted = (await service.get(principal, schedule_id)).schedule
                if terminal_seen:
                    assert persisted.state is ScheduleState.CANCELLED
                terminal_seen = terminal_seen or persisted.state is ScheduleState.CANCELLED
                if persisted.state in {ScheduleState.PAUSED, ScheduleState.CANCELLED}:
                    assert persisted.next_fire_at is None
                async with composition.uow_factory() as uow:
                    occurrences = await uow.schedule_occurrences.list(
                        schedule_id, principal, limit=1000
                    )
                assert all(item.nominal_fire_at <= clock.now() for item in occurrences)

    asyncio.run(exercise_production_lifecycle())


def _principal() -> Principal:
    return Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes={"schedule.read", "schedule.write", "schedule.cancel", "memory.read"},
    )


def _agent() -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="Scheduled lifecycle agent",
        instructions="Follow the scheduled user instruction.",
        model_policy="fake",
        enabled_tools=[],
        policy_profile="default",
        limits=RunLimits(),
    )


def _definition(*, instruction: str = "Summarize project changes.") -> ScheduleDefinition:
    return ScheduleDefinition(
        title="Daily briefing",
        instruction=instruction,
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
        cadence=DailyCadence(local_time=time(9), timezone="America/Los_Angeles"),
        misfire_grace_seconds=60,
        max_consecutive_failures=3,
    )


def _limits() -> ScheduleDefinitionLimits:
    return ScheduleDefinitionLimits(
        max_run_timeout_seconds=60,
        max_misfire_grace_seconds=60,
        max_steps_per_run=4,
        max_model_calls_per_run=4,
        max_tool_calls_per_run=4,
        max_cost_per_run=Decimal("1"),
    )


async def test_lifecycle_is_revisioned_linear_and_never_backfills_paused_time() -> None:
    principal = _principal()
    wakes = 0

    async def wake() -> None:
        nonlocal wakes
        wakes += 1

    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        service = ScheduleService(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            ids=composition.ids,
            wake_worker=wake,
            limits=_limits(),
        )

        with pytest.raises(ScheduleValidationError) as above_ceiling:
            await service.create(
                principal,
                _definition().model_copy(update={"run_timeout_seconds": 61}),
                "too-long",
            )
        assert above_ceiling.value.reason == "schedule.run_timeout_limit"

        with pytest.raises(ScheduleValidationError) as grace_above_ceiling:
            await service.create(
                principal,
                _definition().model_copy(update={"misfire_grace_seconds": 61}),
                "grace-too-long",
            )
        assert grace_above_ceiling.value.reason == "schedule.misfire_grace_limit"

        created = await service.create(principal, _definition(), "daily-briefing")
        replay = await service.create(principal, _definition(), "daily-briefing")
        assert replay.schedule.id == created.schedule.id
        assert replay.replayed is True

        updated = await service.update(
            principal,
            created.schedule.id,
            1,
            _definition(instruction="Use the new pinned instruction."),
        )
        assert updated.schedule.current_revision == 2
        assert updated.revision.instruction == "Use the new pinned instruction."
        async with composition.uow_factory() as uow:
            original = await uow.schedules.get_revision(created.schedule.id, 1, principal)
        assert original.instruction == "Summarize project changes."

        with pytest.raises(ConflictError) as stale:
            await service.update(principal, created.schedule.id, 1, _definition())
        assert stale.value.details == {"current_revision": 2}

        paused = await service.pause(principal, created.schedule.id, 2)
        assert paused.schedule.state is ScheduleState.PAUSED
        assert paused.schedule.next_fire_at is None
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(timedelta(days=30))

        resumed = await service.resume(principal, created.schedule.id, 2)
        assert resumed.schedule.state is ScheduleState.ACTIVE
        assert resumed.schedule.next_fire_at is not None
        assert resumed.schedule.next_fire_at > clock.now()

        cancelled = await service.cancel(principal, created.schedule.id, 2)
        assert cancelled.schedule.state is ScheduleState.CANCELLED
        assert (await service.cancel(principal, created.schedule.id, 2)).schedule == (
            cancelled.schedule
        )
        assert wakes == 3


async def test_revision_updates_only_future_occurrences_and_cancel_leaves_run_unchanged() -> None:
    principal = _principal()
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        service = ScheduleService(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            ids=composition.ids,
            limits=_limits(),
        )
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        created = await service.create(principal, _definition(), "revision-test")
        first = await materializer.materialize(created.schedule.id)
        assert first is not None and first.run_id is not None

        updated = await service.update(
            principal,
            created.schedule.id,
            1,
            _definition(instruction="Use only the second revision."),
        )
        assert updated.schedule.next_fire_at is not None
        assert updated.schedule.next_fire_at > NOW
        async with composition.uow_factory() as uow:
            await uow.runs.transition(first.run_id, RunStatus.QUEUED, RunStatus.RUNNING)
            await uow.runs.transition(first.run_id, RunStatus.RUNNING, RunStatus.COMPLETED)
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(updated.schedule.next_fire_at - NOW)
        second = await materializer.materialize(created.schedule.id)
        assert second is not None
        assert second.schedule_revision == 2
        assert second.session_id is not None
        async with composition.uow_factory() as uow:
            second_events = await uow.events.list_after(second.session_id, 0, principal)
            assert second_events[1].payload["content"] == "Use only the second revision."
            first_run = await uow.runs.get(first.run_id, principal)
        assert first_run.status is RunStatus.COMPLETED

        cancelled = await service.cancel(principal, created.schedule.id, 2)
        assert cancelled.schedule.state is ScheduleState.CANCELLED
        assert second.run_id is not None
        async with composition.uow_factory() as uow:
            unchanged = await uow.runs.get(second.run_id, principal)
        assert unchanged.status is RunStatus.QUEUED
        clock.advance(timedelta(days=1))
        assert await materializer.materialize(created.schedule.id) == second
        async with composition.uow_factory() as uow:
            assert (
                len(await uow.schedule_occurrences.list(created.schedule.id, principal, limit=10))
                == 2
            )
