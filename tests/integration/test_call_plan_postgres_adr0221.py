"""ADR-0221: a call plan's retry fires on PostgreSQL under the scheduler's release grants.

The scheduler role cannot read the call ledger. The maintenance sweep reads the
first call's closed status as the application role, records the step, and the
scheduler then materializes the retry from the step alone.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from agent_core.adapters.determinism import FixedClock
from agent_core.application.call_plans import CallPlanSweep
from agent_core.bootstrap import Composition, build, build_schedule_worker
from agent_core.domain.call_plans import CALL_PLAN_STEP_EVENT
from agent_core.domain.calls import CallRecord
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    CallPlan,
    OnceCadence,
    Schedule,
    ScheduleOccurrence,
    ScheduleRevision,
    ScheduleState,
)
from tests.contract.support import agent
from tests.integration.m2_support import database_settings
from tests.integration.test_schedule_materializer_m11 import NOW
from tests.integration.test_schedule_worker_postgres_m11 import (
    _production_schedule_settings,
    _release_schedule_role,
)

PLAN = CallPlan(retry_every_minutes=2, retry_until=NOW + timedelta(minutes=10))
SCOPES = frozenset({"session.read"})


async def _create_plan(composition: Composition, schedule_id: UUID) -> None:
    principal = composition.principal
    async with composition.uow_factory() as uow:
        await uow.agents.put(agent())
        await uow.schedules.create(
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
                title="Wake Cheryl",
                instruction="Call Cheryl and wake her for her hair appointment.",
                agent_id=agent().id,
                agent_version=agent().version,
                policy_profile=agent().policy_profile,
                requested_scopes=SCOPES,
                limits=RunLimits(
                    max_steps=4, max_model_calls=4, max_tool_calls=4, max_cost=Decimal("1")
                ),
                run_timeout_seconds=60,
                cadence=OnceCadence(at=NOW),
                timezone=None,
                misfire_grace_seconds=60,
                max_consecutive_failures=3,
                created_by_principal_id=principal.principal_id,
                created_at=NOW,
                call_plan=PLAN,
            ),
        )


async def _occurrences(composition: Composition, schedule_id: UUID) -> list[ScheduleOccurrence]:
    async with composition.uow_factory() as uow:
        rows = await uow.schedule_occurrences.list(schedule_id, composition.principal, limit=10)
    return sorted(rows, key=lambda row: row.nominal_fire_at)


async def _seed(composition: Composition, occurrence: ScheduleOccurrence) -> str:
    assert occurrence.session_id is not None and occurrence.run_id is not None
    async with composition.uow_factory() as uow:
        run = await uow.runs.get(occurrence.run_id, composition.principal)
        seed = await uow.events.latest_before(
            occurrence.session_id,
            run.seed_event_sequence + 1,
            "user.message.created",
            composition.principal,
        )
    assert seed is not None and isinstance(seed.payload["content"], str)
    return seed.payload["content"]


async def test_release_privileged_schedule_role_fires_a_call_plan_retry() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        schedule_id = uuid4()
        await _create_plan(composition, schedule_id)
        async with composition.uow_factory() as uow:
            revision = await uow.schedules.get_revision(schedule_id, 1, composition.principal)
        assert revision.call_plan == PLAN

        async with _release_schedule_role() as role_url:
            async with build_schedule_worker(
                settings=_production_schedule_settings(role_url), clock=FixedClock(NOW)
            ) as worker:
                assert await worker.run_once() == 1
            [first] = await _occurrences(composition, schedule_id)
            assert "call 1 of up to 6" in await _seed(composition, first)
            async with composition.uow_factory() as uow:
                waiting = await uow.schedules.get(schedule_id, composition.principal)
                assert waiting.state is ScheduleState.ACTIVE
                assert waiting.next_fire_at is None
                assert first.run_id is not None
                await uow.runs.transition(first.run_id, RunStatus.QUEUED, RunStatus.CANCELLED)
                await uow.calls.put(
                    CallRecord(
                        tenant_id=composition.principal.tenant_id,
                        principal_id=composition.principal.principal_id,
                        kind="dispatch",
                        key=str(uuid4()),
                        revision=1,
                        payload={"run_id": str(first.run_id), "status": "no_answer"},
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    expected_revision=0,
                )

            sweep = CallPlanSweep(
                uow_factory=composition.uow_factory,
                clock=composition.clock,
                ids=composition.ids,
                principal=composition.principal,
            )
            assert await sweep.run_once() == 1
            async with composition.uow_factory() as uow:
                retrying = await uow.schedules.get(schedule_id, composition.principal)
                steps = await uow.process_events.list(CALL_PLAN_STEP_EVENT)
            assert retrying.next_fire_at == NOW + timedelta(minutes=2)
            assert [step.payload["schedule_id"] for step in steps] == [str(schedule_id)]

            async with build_schedule_worker(
                settings=_production_schedule_settings(role_url),
                clock=FixedClock(NOW + timedelta(minutes=2)),
            ) as worker:
                assert await worker.run_once() == 1

        first_again, second = await _occurrences(composition, schedule_id)
        assert first_again == first
        assert second.nominal_fire_at == NOW + timedelta(minutes=2)
        assert "call 2 of up to 6" in await _seed(composition, second)
        assert second.run_id is not None
        async with composition.uow_factory() as uow:
            retry = await uow.runs.get(second.run_id, composition.principal)
            done = await uow.schedules.get(schedule_id, composition.principal)
        assert retry.principal_scopes == SCOPES
        assert done.state is ScheduleState.ACTIVE and done.next_fire_at is None
