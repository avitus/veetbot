"""Milestone 11 terminal-run accounting and auto-pause gate."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.bootstrap import build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import DailyCadence, Schedule, ScheduleRevision, ScheduleState
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from agent_core.scheduling.materializer import ScheduleMaterializer
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)
SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000000711")
AGENT_ID = UUID("00000000-0000-0000-0000-000000000712")


class RecordingScheduleMetrics:
    def __init__(self) -> None:
        self.auto_pauses = 0

    def record_occurrence(self, **_values: object) -> None:
        return

    def record_misfires(self, **_values: object) -> None:
        return

    def record_auto_pause(self) -> None:
        self.auto_pauses += 1


def _principal() -> Principal:
    return Principal(tenant_id="local", principal_id="local-user", roles={"user"}, scopes=set())


def _agent() -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="Accounting agent",
        instructions="Run the task.",
        model_policy="fake-balanced",
        enabled_tools=[],
        policy_profile="default",
        limits=RunLimits(),
    )


async def test_failed_runs_auto_pause_once_at_the_revision_failure_limit() -> None:
    principal = _principal()
    cadence = DailyCadence(local_time=time(16), timezone="UTC")
    schedule = Schedule(
        id=SCHEDULE_ID,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    revision = ScheduleRevision(
        schedule_id=SCHEDULE_ID,
        revision=1,
        title="Failure accounting",
        instruction="Fail deterministically.",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=frozenset(),
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("1"),
        ),
        run_timeout_seconds=60,
        cadence=cadence,
        timezone="UTC",
        misfire_grace_seconds=60,
        max_consecutive_failures=2,
        created_by_principal_id=principal.principal_id,
        created_at=NOW,
    )
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.schedules.create(schedule, revision)
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        accountant = ScheduleOutcomeAccountant(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            ids=composition.ids,
        )
        clock = composition.clock
        assert isinstance(clock, FixedClock)

        for expected_failures in (1, 2):
            occurrence = await materializer.materialize(SCHEDULE_ID)
            assert occurrence is not None and occurrence.run_id is not None
            async with composition.uow_factory() as uow:
                await uow.runs.transition(occurrence.run_id, RunStatus.QUEUED, RunStatus.RUNNING)
                await uow.runs.transition(occurrence.run_id, RunStatus.RUNNING, RunStatus.FAILED)
            assert await accountant.account(occurrence.run_id) is True
            assert await accountant.account(occurrence.run_id) is False
            async with composition.uow_factory() as uow:
                current = await uow.schedules.get(SCHEDULE_ID, principal)
            assert current.consecutive_failures == expected_failures
            if expected_failures == 1:
                assert current.state is ScheduleState.ACTIVE
                clock.advance(timedelta(days=1))

        assert current.state is ScheduleState.PAUSED
        assert current.pause_reason == "failure_limit"
        async with composition.uow_factory() as uow:
            auto_paused = await uow.process_events.list("schedule.auto_paused")
        assert len(auto_paused) == 1


async def test_materializer_failure_path_emits_auto_pause_event_and_metric() -> None:
    principal = _principal()
    schedule = Schedule(
        id=SCHEDULE_ID,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    revision = ScheduleRevision(
        schedule_id=SCHEDULE_ID,
        revision=1,
        title="Disabled principal",
        instruction="Fail authority revalidation.",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=frozenset(),
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("1"),
        ),
        run_timeout_seconds=60,
        cadence=DailyCadence(local_time=time(16), timezone="UTC"),
        timezone="UTC",
        misfire_grace_seconds=60,
        max_consecutive_failures=1,
        created_by_principal_id=principal.principal_id,
        created_at=NOW,
    )
    metrics = RecordingScheduleMetrics()
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.schedules.create(schedule, revision)
        occurrence = await ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal, enabled=False),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
            metrics=metrics,  # type: ignore[arg-type]
        ).materialize(SCHEDULE_ID)

        assert occurrence is not None
        async with composition.uow_factory() as uow:
            [event] = await uow.process_events.list("schedule.auto_paused")
            paused = await uow.schedules.get(SCHEDULE_ID, principal)
        assert event.payload["occurrence_id"] == str(occurrence.id)
        assert paused.state is ScheduleState.PAUSED
        assert metrics.auto_pauses == 1


async def test_executor_completion_hook_resets_schedule_failures() -> None:
    principal = _principal()
    schedule = Schedule(
        id=SCHEDULE_ID,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=NOW,
        consecutive_failures=1,
        created_at=NOW,
        updated_at=NOW,
    )
    revision = ScheduleRevision(
        schedule_id=SCHEDULE_ID,
        revision=1,
        title="Successful accounting",
        instruction="Complete deterministically.",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=frozenset(),
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("1"),
        ),
        run_timeout_seconds=60,
        cadence=DailyCadence(local_time=time(16), timezone="UTC"),
        timezone="UTC",
        misfire_grace_seconds=60,
        max_consecutive_failures=2,
        created_by_principal_id=principal.principal_id,
        created_at=NOW,
    )
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
        script=FakeModelScript(turns=[ScriptedTurn(text="done")]),
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.schedules.create(schedule, revision)
        occurrence = await ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        ).materialize(SCHEDULE_ID)
        assert occurrence is not None and occurrence.run_id is not None

        await composition.executor.execute(occurrence.run_id)

        async with composition.uow_factory() as uow:
            run = await uow.runs.get(occurrence.run_id, principal)
            current = await uow.schedules.get(SCHEDULE_ID, principal)
            accounted = await uow.process_events.list("schedule.run_accounted")
        assert run.status is RunStatus.COMPLETED
        assert current.consecutive_failures == 0
        assert len(accounted) == 1
        assert accounted[0].payload["run_id"] == str(occurrence.run_id)
