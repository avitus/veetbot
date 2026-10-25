"""Cockpit feeds: scheduled publishing and the platform projections (Milestone 35, step 8).

docs/plan/cockpit.md, feeds. A schedule asks for the Cockpit scopes only when
it is created to publish a feed, and only a Cockpit deployment offers that
flag, so `schedule.create`'s current version is unchanged while the flag is
off. The maintenance role projects the platform's own feeds from real state.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from agent_core.bootstrap import build
from agent_core.config import Settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.contract.fake_github import app_private_key
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 10, 24, 17, tzinfo=UTC)
APPROVALS = "veetbot.approvals.pending"
RUNS = "veetbot.runs.activity"
SCHEDULES = "veetbot.schedules.health"
PEOPLE = "veetbot.people.recent"
WEATHER = {
    "title": "Weather for the Cockpit",
    "instruction": "Read the forecast and publish it to the weather feed.",
    "cadence": {"kind": "DAILY", "local_time": "06:00:00", "timezone": "America/Los_Angeles"},
    "publish_cockpit_feed": True,
}


def _settings(*, cockpit: bool) -> Settings:
    return replace(
        memory_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
        cockpit_enabled=cockpit,
    )


async def test_without_cockpit_schedule_create_keeps_its_current_version() -> None:
    async with build(
        settings=_settings(cockpit=False), script=FakeModelScript(turns=[])
    ) as composition:
        spec = composition.tool_pipeline._registry.get("schedule.create").spec
    assert spec.version == "1.4.0"
    assert "publish_cockpit_feed" not in spec.input_schema["properties"]


async def test_a_schedule_created_to_publish_a_feed_asks_for_the_cockpit_scopes() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create", arguments=WEATHER, call_id="weather-feed"
                    )
                ]
            ),
            ScriptedTurn(text="Every morning at six the weather feed is refreshed."),
        ]
    )
    async with build(
        settings=_settings(cockpit=True),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["schedule.create"],
    ) as composition:
        registry = composition.tool_pipeline._registry
        current = registry.get("schedule.create").spec
        pinned = registry.get("schedule.create", "1.4.0").spec
        run_id = await composition.runs.submit("Refresh the weather on my Cockpit every morning.")
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        completed = await composition.runs.wait_terminal(run_id)
        page = await composition.schedules.list(composition.principal, 10, None)

    assert current.version == "1.5.0"
    assert current.input_schema["properties"]["publish_cockpit_feed"] == {"type": "boolean"}
    assert "publish_cockpit_feed" not in pinned.input_schema["properties"]
    assert approval.arguments["requested_scopes"] == ["cockpit.read", "cockpit.write"]
    assert approval.arguments["publish_cockpit_feed"] is True
    assert completed.status is RunStatus.COMPLETED
    [record] = page.items
    assert record.revision.requested_scopes == frozenset({"cockpit.read", "cockpit.write"})
    assert record.schedule.next_fire_at is not None
    assert record.schedule.next_fire_at - NOW < timedelta(days=1)


async def test_the_maintenance_role_projects_the_platform_feeds_from_real_state(
    tmp_path: Path,
) -> None:
    """One projector pass over a real composition: a pending approval shows on two feeds."""

    key = tmp_path / "cockpit-github-app"
    key.write_bytes(app_private_key())
    key.chmod(0o400)
    settings = replace(
        _settings(cockpit=True),
        cockpit_github_repository="avitus/veetbot",
        cockpit_github_app_id=123456,
        cockpit_github_installation_id=7890,
        cockpit_github_key_file=key,
        people_enabled=True,
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "hello"},
                        call_id="needs-approval",
                    )
                ]
            )
        ]
    )
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["demo.external_write"],
    ) as composition:
        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit("Write hello to the demo.", session_id)
        assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        worker = composition.maintenance_factory()
        published = await worker._sweep_cockpit_feeds()  # type: ignore[attr-defined]
        again = await worker._sweep_cockpit_feeds()  # type: ignore[attr-defined]
        async with composition.uow_factory() as uow:
            latest = await uow.cockpit_feeds.latest(
                composition.principal, [APPROVALS, RUNS, SCHEDULES, PEOPLE]
            )
    assert published == 4
    assert again == 0
    approvals = cast(dict[str, Any], latest[APPROVALS].data)
    assert approvals["count"] == 1
    assert approvals["items"][0]["kind"] == "Demo"
    assert latest[APPROVALS].expected_interval_seconds == 300
    assert cast(dict[str, Any], latest[RUNS].data)["waiting"] == 1
    assert latest[SCHEDULES].data == {
        "onTime": 0,
        "total": 0,
        "failed": 0,
        "paused": 0,
        "items": [],
    }
    assert latest[PEOPLE].data == {"items": []}
