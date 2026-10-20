"""ADR-0225 part C: Run now starts one manual occurrence of a recurring schedule.

The run is an ordinary materialized occurrence with trigger MANUAL: the
cadence's next firing does not move, the result never changes the
consecutive-failure count, a cadence instant due while it runs is skipped as
an overlap, and the route is bounded by schedule.run, the revision, a
one-active-run rule and five requests a day.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    DailyCadence,
    OccurrenceDisposition,
    OnceCadence,
    Schedule,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from agent_core.scheduling.materializer import MANUAL_RUNS_PER_DAY, ScheduleMaterializer
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 10, 20, 15, tzinfo=UTC)
AGENT_ID = UUID("00000000-0000-0000-0000-000000000811")
SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000000812")
OWNER = Principal(
    tenant_id="local", principal_id="local-user", roles={"user"}, scopes=set(PLATFORM_SCOPES)
)


def _agent() -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="Run-now agent",
        instructions="Run the task.",
        model_policy="fake",
        enabled_tools=[],
        policy_profile="default",
        limits=RunLimits(),
    )


def _rows(*, once: bool = False, failures: int = 0) -> tuple[Schedule, ScheduleRevision]:
    cadence = (
        OnceCadence(at=NOW + timedelta(days=2))
        if once
        else DailyCadence(local_time=time(9), timezone="UTC")
    )
    next_fire = NOW + timedelta(days=2) if once else NOW.replace(hour=9) + timedelta(days=1)
    schedule = Schedule(
        id=SCHEDULE_ID,
        tenant_id=OWNER.tenant_id,
        principal_id=OWNER.principal_id,
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=next_fire,
        consecutive_failures=failures,
        created_at=NOW,
        updated_at=NOW,
    )
    revision = ScheduleRevision(
        schedule_id=SCHEDULE_ID,
        revision=1,
        title="Snow check",
        instruction="Check the snow report.",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=frozenset(),
        limits=RunLimits(max_steps=4, max_model_calls=4, max_tool_calls=4, max_cost=Decimal("1")),
        run_timeout_seconds=60,
        cadence=cadence,
        timezone=None if once else "UTC",
        misfire_grace_seconds=3600,
        max_consecutive_failures=2,
        created_by_principal_id=OWNER.principal_id,
        created_at=NOW,
    )
    return schedule, revision


@asynccontextmanager
async def _api(
    *, once: bool = False, principal: Principal = OWNER
) -> AsyncIterator[tuple[httpx.AsyncClient, Composition]]:
    async with build(
        settings=replace(memory_settings(), schedule_api_enabled=True),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=OWNER,
    ) as composition:
        schedule, revision = _rows(once=once)
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.schedules.create(schedule, revision)
        app = create_app(
            composition.services,
            composition.settings,
            principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            yield client, composition


async def _run(client: httpx.AsyncClient, key: str, revision: int = 1) -> httpx.Response:
    return await client.post(
        f"/v1/schedules/{SCHEDULE_ID}/run",
        json={"expected_revision": revision},
        headers={"Idempotency-Key": key},
    )


async def _finish(composition: Composition, run_id: str, status: RunStatus) -> None:
    async with composition.uow_factory() as uow:
        await uow.runs.transition(UUID(run_id), RunStatus.QUEUED, RunStatus.RUNNING)
        await uow.runs.transition(UUID(run_id), RunStatus.RUNNING, status)


async def test_run_now_starts_a_manual_occurrence_and_leaves_the_cadence_alone() -> None:
    async with _api() as (client, composition):
        before = (await client.get(f"/v1/schedules/{SCHEDULE_ID}")).json()["schedule"]
        started = await _run(client, "run-1")
        assert started.status_code == 201, started.text
        occurrence: dict[str, Any] = started.json()
        assert occurrence["trigger"] == "MANUAL"
        assert occurrence["disposition"] == "MATERIALIZED"
        assert occurrence["nominal_fire_at"] == NOW.isoformat().replace("+00:00", "Z")
        assert occurrence["run_id"] and occurrence["session_id"]
        after = (await client.get(f"/v1/schedules/{SCHEDULE_ID}")).json()["schedule"]
        assert after["next_fire_at"] == before["next_fire_at"]
        async with composition.uow_factory() as uow:
            run = await uow.runs.get(UUID(occurrence["run_id"]), OWNER)
        assert run.status is RunStatus.QUEUED

        replay = await _run(client, "run-1")
        assert replay.status_code == 200
        assert replay.json()["id"] == occurrence["id"]

        busy = await _run(client, "run-2")
        assert busy.status_code == 409
        assert busy.json()["error"]["details"]["reason"] == "schedule.run_active"

        history = (await client.get(f"/v1/schedules/{SCHEDULE_ID}/occurrences")).json()
        assert [item["trigger"] for item in history["items"]] == ["MANUAL"]


async def test_run_now_needs_its_own_scope_and_the_current_revision() -> None:
    writer = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes={"schedule.read", "schedule.write", "schedule.cancel"},
    )
    async with _api(principal=writer) as (client, _):
        assert (await _run(client, "run-1")).status_code == 403
    async with _api() as (client, _):
        stale = await _run(client, "run-1", revision=2)
        assert stale.status_code == 409
        assert stale.json()["error"]["details"]["reason"] == "schedule.revision_conflict"
        missing_key = await client.post(
            f"/v1/schedules/{SCHEDULE_ID}/run", json={"expected_revision": 1}
        )
        assert missing_key.status_code in {400, 422}


async def test_a_one_time_schedule_runs_only_at_its_time() -> None:
    async with _api(once=True) as (client, _):
        refused = await _run(client, "run-1")
        assert refused.status_code == 409
        assert refused.json()["error"]["details"]["reason"] == "schedule.run_now_one_time"


async def test_a_paused_schedule_can_run_now_but_a_stopped_one_cannot() -> None:
    async with _api() as (client, composition):
        paused = await client.post(
            f"/v1/schedules/{SCHEDULE_ID}/pause", json={"expected_revision": 1}
        )
        assert paused.status_code == 200
        started = await _run(client, "run-1")
        assert started.status_code == 201, started.text
        await _finish(composition, started.json()["run_id"], RunStatus.COMPLETED)
        stopped = await client.delete(
            f"/v1/schedules/{SCHEDULE_ID}", params={"expected_revision": 1}
        )
        assert stopped.status_code == 200
        refused = await _run(client, "run-2")
        assert refused.status_code == 409
        assert refused.json()["error"]["details"]["reason"] == "schedule.terminal"


async def test_five_requests_a_day_then_429() -> None:
    async with _api() as (client, composition):
        for index in range(MANUAL_RUNS_PER_DAY):
            started = await _run(client, f"run-{index}")
            assert started.status_code == 201, started.text
            await _finish(composition, started.json()["run_id"], RunStatus.COMPLETED)
        limited = await _run(client, "run-extra")
        assert limited.status_code == 429
        assert limited.json()["error"]["details"]["reason"] == "schedule.run_now_daily_limit"
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(timedelta(days=1, seconds=1))
        assert (await _run(client, "run-next-day")).status_code == 201


async def test_a_manual_result_never_moves_the_failure_count_or_pauses() -> None:
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=OWNER,
    ) as composition:
        schedule, revision = _rows(failures=1)
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.schedules.create(schedule, revision)
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(OWNER),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        accountant = ScheduleOutcomeAccountant(
            uow_factory=composition.uow_factory, clock=composition.clock, ids=composition.ids
        )
        occurrence, replayed = await materializer.materialize_manual(
            OWNER, SCHEDULE_ID, expected_revision=1, request_key="manual-1"
        )
        assert not replayed and occurrence.run_id is not None
        await _finish(composition, str(occurrence.run_id), RunStatus.FAILED)
        assert await accountant.account(occurrence.run_id)
        async with composition.uow_factory() as uow:
            current = await uow.schedules.get(SCHEDULE_ID, OWNER)
        assert current.consecutive_failures == 1
        assert current.state is ScheduleState.ACTIVE


async def test_a_cadence_instant_due_during_a_manual_run_is_skipped_as_overlap() -> None:
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=OWNER,
    ) as composition:
        schedule, revision = _rows()
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            await uow.schedules.create(schedule, revision)
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(OWNER),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        manual, _ = await materializer.materialize_manual(
            OWNER, SCHEDULE_ID, expected_revision=1, request_key="manual-1"
        )
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(schedule.next_fire_at - NOW)  # type: ignore[operator]
        scheduled = await materializer.materialize(SCHEDULE_ID)
        assert scheduled is not None
        assert scheduled.disposition is OccurrenceDisposition.SKIPPED_OVERLAP
        assert scheduled.id != manual.id
