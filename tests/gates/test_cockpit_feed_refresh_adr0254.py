"""ADR-0254: Cockpit feeds refresh every minute to every day, and on entry.

docs/plan/cockpit.md (assistant feeds, refresh on entry) and
docs/plan/scheduling.md (the INTERVAL cadence). Only a feed-keeping schedule
may fire on an interval, the tools that offer it leave every pinned version as
it was, feed upkeep stays quiet, and `POST /v1/cockpit/refresh` starts one run
of each active feed-keeping schedule, at most once a minute.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.recurrence import RecurrenceCalculator
from agent_core.domain.runs import Run, RunLimits, RunStatus
from agent_core.domain.schedules import (
    Cadence,
    DailyCadence,
    IntervalCadence,
    OccurrenceDisposition,
    OccurrenceTrigger,
    OnceCadence,
    Schedule,
    ScheduleNotify,
    ScheduleOccurrence,
    SchedulePauseReason,
    ScheduleRefreshOutcome,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from agent_core.scheduling.materializer import MANUAL_RUNS_PER_DAY, ScheduleMaterializer
from agent_core.tools.registry import RegisteredTool
from agent_core.tools.validation import validate_and_normalize
from tests.contract.support import tool_context
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 10, 26, 16, 7, 30, tzinfo=UTC)
AGENT_ID = UUID("00000000-0000-0000-0000-000000025400")
OWNER = Principal(
    tenant_id="local", principal_id="local-user", roles={"user"}, scopes=set(PLATFORM_SCOPES)
)
FEED = frozenset({"cockpit.read", "cockpit.write"})
QUARTER_HOUR = {"kind": "INTERVAL", "every_minutes": 15, "timezone": "America/Los_Angeles"}
PULL_REQUESTS = {
    "title": "Pull requests for the Cockpit",
    "instruction": "Read the open pull requests and publish them to the pull-request feed.",
    "cadence": QUARTER_HOUR,
    "publish_cockpit_feed": True,
}


def _id(number: int) -> UUID:
    return UUID(int=0x25400 * 0x1000 + number)


def _agent() -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="Feed agent",
        instructions="Keep the feed current.",
        model_policy="fake",
        enabled_tools=[],
        policy_profile="default",
        limits=RunLimits(),
    )


def _rows(
    schedule_id: UUID,
    *,
    cadence: Cadence | None = None,
    scopes: frozenset[str] = FEED,
    state: ScheduleState = ScheduleState.ACTIVE,
) -> tuple[Schedule, ScheduleRevision]:
    cadence = cadence or IntervalCadence(every_minutes=15, timezone="UTC")
    schedule = Schedule(
        id=schedule_id,
        tenant_id=OWNER.tenant_id,
        principal_id=OWNER.principal_id,
        state=state,
        pause_reason=SchedulePauseReason.USER if state is ScheduleState.PAUSED else None,
        current_revision=1,
        next_fire_at=(
            RecurrenceCalculator.next_after(cadence, NOW) if state is ScheduleState.ACTIVE else None
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    revision = ScheduleRevision(
        schedule_id=schedule_id,
        revision=1,
        title=f"Feed {schedule_id.int % 100}",
        instruction="Publish the feed.",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=scopes,
        limits=RunLimits(max_steps=4, max_model_calls=4, max_tool_calls=4, max_cost=Decimal("1")),
        run_timeout_seconds=60,
        cadence=cadence,
        timezone=None if isinstance(cadence, OnceCadence) else cadence.timezone,
        misfire_grace_seconds=3600,
        max_consecutive_failures=2,
        created_by_principal_id=OWNER.principal_id,
        created_at=NOW,
    )
    return schedule, revision


def _definition(cadence: dict[str, object], scopes: list[str]) -> dict[str, object]:
    return {
        "title": "Pull requests",
        "instruction": "Publish the open pull requests.",
        "agent_id": str(AGENT_ID),
        "agent_version": "1.0.0",
        "policy_profile": "default",
        "requested_scopes": scopes,
        "limits": {"max_steps": 4, "max_model_calls": 4, "max_tool_calls": 4, "max_cost": "1"},
        "run_timeout_seconds": 60,
        "cadence": cadence,
        "misfire_grace_seconds": 60,
        "max_consecutive_failures": 3,
    }


class _Recorder:
    """Records which schedule notifications the scheduler asked for."""

    def __init__(self) -> None:
        self.skipped: list[ScheduleOccurrence] = []
        self.finished: list[Run] = []

    async def for_schedule_occurrence(self, uow: Any, **kwargs: Any) -> bool:
        self.skipped.append(kwargs["occurrence"])
        return True

    async def for_schedule_run_accounted(self, uow: Any, **kwargs: Any) -> bool:
        self.finished.append(kwargs["run"])
        return True


@asynccontextmanager
async def _composition(
    *, cockpit: bool = True, principal: Principal = OWNER
) -> AsyncIterator[tuple[Composition, httpx.AsyncClient]]:
    settings = replace(
        memory_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
        cockpit_enabled=cockpit,
    )
    async with build(
        settings=settings,
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=OWNER,
        script=FakeModelScript(turns=[]),
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        app = create_app(
            composition.services,
            composition.settings,
            principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            yield composition, client


def _materializer(composition: Composition, recorder: _Recorder | None = None) -> Any:
    return ScheduleMaterializer(
        uow_factory=composition.uow_factory,
        principals=StaticSchedulePrincipalDirectory(OWNER),
        admission=AllowScheduleAdmissionController(),
        clock=composition.clock,
        ids=composition.ids,
        seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        notification_producer=recorder,
    )


async def _create(composition: Composition, *rows: tuple[Schedule, ScheduleRevision]) -> None:
    async with composition.uow_factory() as uow:
        for schedule, revision in rows:
            await uow.schedules.create(schedule, revision)


async def _finish(composition: Composition, run_id: UUID, status: RunStatus) -> None:
    async with composition.uow_factory() as uow:
        await uow.runs.transition(run_id, RunStatus.QUEUED, RunStatus.RUNNING)
        await uow.runs.transition(run_id, RunStatus.RUNNING, status)


async def _latest(composition: Composition, schedule_id: UUID) -> ScheduleOccurrence:
    async with composition.uow_factory() as uow:
        occurrence = await uow.schedule_occurrences.latest_materialized(schedule_id)
    assert occurrence is not None and occurrence.run_id is not None
    return occurrence


def _advance(composition: Composition, delta: timedelta) -> None:
    clock = composition.clock
    assert isinstance(clock, FixedClock)
    clock.advance(delta)


async def _refresh(client: httpx.AsyncClient) -> dict[str, str]:
    answer = await client.post("/v1/cockpit/refresh")
    assert answer.status_code == 202, answer.text
    assert answer.headers["Cache-Control"] == "private, no-store"
    return {item["schedule_id"]: item["outcome"] for item in answer.json()["schedules"]}


@pytest.mark.parametrize("trigger", ["scheduled", "entry", "manual"])
@pytest.mark.parametrize("job", [False, True])
async def test_feed_refreshes_have_audit_history_but_never_create_chats(
    trigger: str, job: bool
) -> None:
    """Keep every refresh trigger auditable without adding a conversation."""
    from agent_core.domain.schedules import JobTask

    async with _composition() as (composition, client):
        feed, revision = _rows(_id(901))
        if job:
            revision = revision.model_copy(
                update={
                    "task": JobTask(slug="feed-job", revision_id=_id(910), grant_id=_id(911)),
                    "requested_scopes": FEED | {"jobs.run"},
                }
            )
        ordinary = _rows(
            _id(902), cadence=DailyCadence(local_time=time(17), timezone="UTC"), scopes=frozenset()
        )
        await _create(composition, (feed, revision), ordinary)
        materializer = _materializer(composition)
        if trigger == "entry":
            await _refresh(client)
        elif trigger == "manual":
            await materializer.materialize_manual(
                OWNER, feed.id, expected_revision=1, request_key="refresh"
            )
        else:
            assert feed.next_fire_at is not None
            _advance(composition, feed.next_fire_at - composition.clock.now())
            await materializer.materialize(feed.id)
        refresh = await _latest(composition, feed.id)
        normal, _ = await materializer.materialize_manual(
            OWNER, ordinary[0].id, expected_revision=1, request_key="briefing"
        )
        # Audit and failure inspection remain available, independently of Chat.
        async with composition.uow_factory() as uow:
            assert refresh.session_id is not None
            assert (await uow.sessions.get(refresh.session_id, OWNER)).id == refresh.session_id
        response = await client.get("/v1/sessions")
        assert response.status_code == 200, response.text
        assert {item["id"] for item in response.json()["items"]} == {str(normal.session_id)}


async def test_cockpit_create_offers_an_interval_to_a_feed_and_keeps_1_5_0_pinned() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create", arguments=PULL_REQUESTS, call_id="feed"
                    )
                ]
            ),
            ScriptedTurn(text="The pull-request feed refreshes every fifteen minutes."),
        ]
    )
    async with build(
        settings=replace(
            memory_settings(),
            schedule_api_enabled=True,
            schedule_worker_enabled=True,
            cockpit_enabled=True,
        ),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["schedule.create"],
    ) as composition:
        registry = composition.tool_pipeline._registry
        current = registry.get("schedule.create").spec
        pinned = registry.get("schedule.create", "1.5.0").spec
        run_id = await composition.runs.submit("Refresh my pull requests every fifteen minutes.")
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        completed = await composition.runs.wait_terminal(run_id)
        page = await composition.schedules.list(composition.principal, 10, None)

    assert current.version == "1.6.0"
    assert "INTERVAL" in json.dumps(current.input_schema)
    assert "INTERVAL" not in json.dumps(pinned.input_schema)
    assert cast(dict[str, object], approval.arguments["cadence"])["kind"] == "INTERVAL"
    assert completed.status is RunStatus.COMPLETED
    [record] = page.items
    assert record.revision.cadence == IntervalCadence(
        every_minutes=15, timezone="America/Los_Angeles"
    )
    assert record.revision.requested_scopes == FEED
    assert record.revision.notify is ScheduleNotify.ATTENTION_ONLY
    assert record.schedule.next_fire_at == datetime(2026, 10, 26, 16, 15, tzinfo=UTC)


async def test_the_interval_tool_versions_are_offered_only_where_the_cockpit_is() -> None:
    async with _composition(cockpit=False) as (composition, _):
        registry = composition.tool_pipeline._registry
        assert registry.get("schedule.create").spec.version == "1.4.0"
        assert registry.get("schedule.update").spec.version == "1.0.1"
        assert registry.get("schedule.list").spec.version == "1.1.0"
        assert registry.get("schedule.list", "1.0.2").spec.version == "1.0.2"
    async with _composition() as (composition, _):
        registry = composition.tool_pipeline._registry
        assert registry.get("schedule.update").spec.version == "1.1.0"
        assert "INTERVAL" in json.dumps(registry.get("schedule.update").spec.input_schema)
        assert "INTERVAL" not in json.dumps(
            registry.get("schedule.update", "1.0.1").spec.input_schema
        )


def test_the_schedule_role_vouches_for_feed_scopes_only_where_the_cockpit_is() -> None:
    from agent_core.bootstrap import _schedule_principal

    base = replace(memory_settings(), auth_scopes=frozenset({"schedule.read", "schedule.write"}))
    assert not FEED & _schedule_principal(base).scopes
    assert _schedule_principal(replace(base, cockpit_enabled=True)).scopes >= FEED


async def test_only_a_feed_keeping_schedule_may_fire_on_an_interval() -> None:
    async with _composition() as (composition, client):
        refused = await client.post(
            "/v1/schedules",
            json=_definition(QUARTER_HOUR, []),
            headers={"Idempotency-Key": "plain"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["reason"] == "schedule.interval_needs_feed"

        created = await client.post(
            "/v1/schedules",
            json=_definition(QUARTER_HOUR, sorted(FEED)),
            headers={"Idempotency-Key": "feed"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["revision"]["cadence"] == QUARTER_HOUR
        assert created.json()["schedule"]["next_fire_at"] == "2026-10-26T16:15:00Z"

        daily: dict[str, object] = {"kind": "DAILY", "local_time": "09:00:00", "timezone": "UTC"}
        plain = await client.post(
            "/v1/schedules", json=_definition(daily, []), headers={"Idempotency-Key": "daily"}
        )
        assert plain.status_code == 201, plain.text
        patched = await client.patch(
            f"/v1/schedules/{plain.json()['schedule']['id']}",
            json={"expected_revision": 1, "definition": _definition(QUARTER_HOUR, [])},
        )
        assert patched.status_code == 422, patched.text
        assert patched.json()["error"]["details"]["reason"] == "schedule.interval_needs_feed"

        tool = registry_tool(composition, "schedule.create")
        context = replace(tool_context(), principal=OWNER, idempotency_key="no-feed")
        without_feed = await tool.execute({**PULL_REQUESTS, "publish_cockpit_feed": False}, context)
        assert without_feed.ok is False and without_feed.failure is not None
        assert without_feed.failure.reason_code == "schedule.interval_needs_feed"
        page = await composition.schedules.list(OWNER, 10, None)
    assert len(page.items) == 2


def registry_tool(composition: Composition, name: str, version: str | None = None) -> Any:
    registered = cast(RegisteredTool, composition.tool_pipeline._registry.get(name, version))
    return registered.implementation


async def test_list_and_update_carry_an_interval() -> None:
    async with _composition() as (composition, _):
        schedule, revision = _rows(
            _id(1), cadence=DailyCadence(local_time=time(16), timezone="UTC")
        )
        await _create(composition, (schedule, revision))
        update = registry_tool(composition, "schedule.update")
        context = replace(tool_context(), principal=OWNER, idempotency_key="every-ten")
        updated = await update.execute(
            {
                "schedule_id": str(schedule.id),
                "expected_revision": 1,
                "cadence": {"kind": "INTERVAL", "every_minutes": 10, "timezone": "UTC"},
            },
            context,
        )
        assert updated.ok is True, updated.failure
        assert updated.structured is not None
        assert updated.structured["next_fire_at"] == "2026-10-26T16:10:00+00:00"

        listing = registry_tool(composition, "schedule.list")
        listed = await listing.execute({}, replace(tool_context(), principal=OWNER))
        assert listed.ok is True and listed.structured is not None
        validate_and_normalize(listed.structured, listing.spec.output_schema)
        [item] = listed.structured["items"]
        assert item["cadence"] == {"kind": "INTERVAL", "every_minutes": 10, "timezone": "UTC"}


async def test_an_interval_schedule_fires_on_its_instants_and_skips_overlap_quietly() -> None:
    async with _composition() as (composition, _):
        recorder = _Recorder()
        interval, interval_revision = _rows(_id(1))
        daily, daily_revision = _rows(
            _id(2), cadence=DailyCadence(local_time=time(16, 15), timezone="UTC")
        )
        await _create(composition, (interval, interval_revision), (daily, daily_revision))
        materializer = _materializer(composition, recorder)

        _advance(composition, datetime(2026, 10, 26, 16, 15, tzinfo=UTC) - NOW)
        fired = await materializer.materialize(interval.id)
        assert fired is not None and fired.disposition is OccurrenceDisposition.MATERIALIZED
        assert fired.nominal_fire_at == datetime(2026, 10, 26, 16, 15, tzinfo=UTC)
        await materializer.materialize(daily.id)

        _advance(composition, timedelta(minutes=15))
        overlapped = await materializer.materialize(interval.id)
        assert overlapped is not None
        assert overlapped.disposition is OccurrenceDisposition.SKIPPED_OVERLAP
        assert recorder.skipped == []
        async with composition.uow_factory() as uow:
            current = await uow.schedules.get(interval.id, OWNER)
        assert current.next_fire_at == datetime(2026, 10, 26, 16, 45, tzinfo=UTC)

        # A calendar schedule's overlap still tells the owner, as before.
        _advance(composition, timedelta(days=1))
        skipped = await materializer.materialize(daily.id)
        assert skipped is not None
        assert skipped.disposition is OccurrenceDisposition.SKIPPED_OVERLAP
        assert [occurrence.id for occurrence in recorder.skipped] == [skipped.id]


async def test_refresh_starts_each_active_feed_schedule_at_most_once_a_minute() -> None:
    async with _composition() as (composition, client):
        daily_feed = _rows(_id(1), cadence=DailyCadence(local_time=time(16), timezone="UTC"))
        interval_feed = _rows(_id(2))
        plain = _rows(_id(3), scopes=frozenset())
        paused = _rows(_id(4), state=ScheduleState.PAUSED)
        once = _rows(_id(5), cadence=OnceCadence(at=NOW + timedelta(hours=1)))
        await _create(composition, daily_feed, interval_feed, plain, paused, once)
        feeds = (daily_feed[0].id, interval_feed[0].id)

        started = await _refresh(client)
        assert started == {str(schedule_id): "started" for schedule_id in feeds}
        for schedule_id in feeds:
            occurrence = await _latest(composition, schedule_id)
            assert occurrence.trigger is OccurrenceTrigger.MANUAL
            assert occurrence.manual_request_key == "cockpit.refresh:2026-10-26T16:07Z"
        async with composition.uow_factory() as uow:
            interval_now = await uow.schedules.get(interval_feed[0].id, OWNER)
        assert interval_now.next_fire_at == interval_feed[0].next_fire_at

        assert await _refresh(client) == {str(schedule_id): "running" for schedule_id in feeds}
        for schedule_id in feeds:
            run_id = (await _latest(composition, schedule_id)).run_id
            assert run_id is not None
            await _finish(composition, run_id, RunStatus.COMPLETED)
        _advance(composition, timedelta(seconds=40))
        assert await _refresh(client) == {str(schedule_id): "recent" for schedule_id in feeds}
        _advance(composition, timedelta(seconds=21))
        assert await _refresh(client) == {str(schedule_id): "started" for schedule_id in feeds}


async def test_refresh_needs_cockpit_write_and_the_cockpit() -> None:
    reader = OWNER.model_copy(update={"scopes": {"cockpit.read", "schedule.read"}})
    async with _composition(principal=reader) as (_, client):
        assert (await client.post("/v1/cockpit/refresh")).status_code == 403
    async with _composition(cockpit=False) as (_, client):
        assert (await client.post("/v1/cockpit/refresh")).status_code == 404


async def test_a_refused_check_is_reported_and_the_other_feeds_still_start() -> None:
    async with _composition() as (composition, client):
        healthy = _rows(_id(1))
        revoked = _rows(_id(2), scopes=FEED | {"mcp.retired_server.use"})
        await _create(composition, healthy, revoked)
        answer = await client.post("/v1/cockpit/refresh")
        assert answer.status_code == 202, answer.text
        outcomes = {item["schedule_id"]: item for item in answer.json()["schedules"]}
        assert outcomes[str(healthy[0].id)] == {
            "schedule_id": str(healthy[0].id),
            "outcome": "started",
            "reason": None,
        }
        assert outcomes[str(revoked[0].id)] == {
            "schedule_id": str(revoked[0].id),
            "outcome": "refused",
            "reason": "schedule.scope_revoked",
        }


async def test_refresh_runs_leave_run_now_its_five_a_day_and_stay_quiet() -> None:
    async with _composition() as (composition, _):
        recorder = _Recorder()
        await _create(composition, _rows(_id(1)))
        materializer = _materializer(composition, recorder)
        accountant = ScheduleOutcomeAccountant(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            ids=composition.ids,
            notification_producer=recorder,
        )
        for _ in range(MANUAL_RUNS_PER_DAY + 1):
            [refreshed] = await materializer.refresh_feeds(OWNER)
            assert refreshed.outcome is ScheduleRefreshOutcome.STARTED
            run_id = (await _latest(composition, _id(1))).run_id
            assert run_id is not None
            await _finish(composition, run_id, RunStatus.COMPLETED)
            assert await accountant.account(run_id)
            _advance(composition, timedelta(minutes=1))
        assert recorder.finished == []

        manual, replayed = await materializer.materialize_manual(
            OWNER, _id(1), expected_revision=1, request_key="owner-asked"
        )
        assert not replayed and manual.trigger is OccurrenceTrigger.MANUAL

        _advance(composition, timedelta(minutes=1))
        assert manual.run_id is not None
        await _finish(composition, manual.run_id, RunStatus.COMPLETED)
        [refreshed] = await materializer.refresh_feeds(OWNER)
        assert refreshed.outcome is ScheduleRefreshOutcome.STARTED
        failed = (await _latest(composition, _id(1))).run_id
        assert failed is not None
        await _finish(composition, failed, RunStatus.FAILED)
        assert await accountant.account(failed)
        assert [run.id for run in recorder.finished] == [failed]
