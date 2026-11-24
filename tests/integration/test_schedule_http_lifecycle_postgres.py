"""A schedule is edited, run on request, paused, resumed and stopped over HTTP on PostgreSQL.

Guards docs/plan/scheduling.md "Public API" and the closed transition table
above it: `PATCH` writes revision N+1 and a stale `expected_revision` is a
conflict that carries the current revision and changes nothing; `POST
/v1/schedules/{id}/run` (ADR-0225 part C) starts one `MANUAL` occurrence that
the durable worker really executes under the current revision, replays by
key, refuses a second run while one is active, and never moves
`next_fire_at`; a paused schedule may still run on request and resumes at an
instant strictly after the resume time; `DELETE` stops future materialization,
is idempotent, and leaves the occurrence history readable after a restart.
The in-memory gates prove each rule alone; this journey chains them through
the routes, the materializer, the worker and the PostgreSQL stores.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.scheduling.worker import ScheduleWorker
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 20, 15, tzinfo=UTC)
AGENT_ID = UUID("00000000-0000-0000-0000-000000001121")
FIRST_INSTRUCTION = "Summarize yesterday's project changes."
EDITED_INSTRUCTION = "Summarize yesterday's project changes and list open reviews."


def _definition(instruction: str) -> dict[str, Any]:
    return {
        "title": "Daily project briefing",
        "instruction": instruction,
        "agent_id": str(AGENT_ID),
        "agent_version": "1.0.0",
        "policy_profile": "default",
        "requested_scopes": ["workspace.read"],
        "limits": {
            "max_steps": 4,
            "max_model_calls": 4,
            "max_tool_calls": 4,
            "max_cost": str(Decimal("1")),
        },
        "run_timeout_seconds": 3600,
        "cadence": {"kind": "DAILY", "local_time": time(9).isoformat(), "timezone": "UTC"},
        "misfire_grace_seconds": 60,
        "max_consecutive_failures": 3,
    }


async def _seed_agent(composition: Composition) -> None:
    async with composition.uow_factory() as uow:
        await uow.agents.put(
            AgentSpec(
                id=AGENT_ID,
                version="1.0.0",
                name="Schedule lifecycle agent",
                instructions="Follow the scheduled instruction.",
                model_policy="fake-balanced",
                enabled_tools=[],
                policy_profile="default",
                limits=RunLimits(),
            )
        )


async def _run_now(
    client: httpx.AsyncClient, schedule_id: UUID, key: str, revision: int
) -> httpx.Response:
    return await client.post(
        f"/v1/schedules/{schedule_id}/run",
        json={"expected_revision": revision},
        headers={"Idempotency-Key": key},
    )


def _reason(response: httpx.Response) -> str:
    return str(response.json()["error"]["details"]["reason"])


async def _schedule(client: httpx.AsyncClient, schedule_id: UUID) -> dict[str, Any]:
    response = await client.get(f"/v1/schedules/{schedule_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_edited_schedule_runs_on_request_then_pauses_resumes_and_stops() -> None:
    """One schedule's whole owner-driven life, with both manual runs really executed."""
    settings = replace(database_settings(), schedule_api_enabled=True, schedule_worker_enabled=True)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(text="Two changes landed; one review is open."),
            ScriptedTurn(text="Nothing changed while paused."),
        ]
    )
    clock = FixedClock(NOW)
    async with (
        build(settings=settings, storage="postgres", script=script, clock=clock) as app,
        api_client(app) as client,
    ):
        await _seed_agent(app)
        created = await client.post(
            "/v1/schedules",
            json=_definition(FIRST_INSTRUCTION),
            headers={"Idempotency-Key": "lifecycle-create"},
        )
        assert created.status_code == 201, created.text
        schedule_id = UUID(created.json()["schedule"]["id"])
        first_fire = created.json()["schedule"]["next_fire_at"]
        assert datetime.fromisoformat(first_fire) == datetime(2026, 10, 21, 9, tzinfo=UTC)

        # A stale writer loses and learns the current revision; nothing changes.
        stale = await client.patch(
            f"/v1/schedules/{schedule_id}",
            json={"expected_revision": 2, "definition": _definition("A stale edit.")},
        )
        assert stale.status_code == 409, stale.text
        assert _reason(stale) == "schedule.revision_conflict"
        assert stale.json()["error"]["details"]["current_revision"] == 1
        unchanged = await _schedule(client, schedule_id)
        assert unchanged["schedule"]["current_revision"] == 1
        assert unchanged["revision"]["instruction"] == FIRST_INSTRUCTION

        edited = await client.patch(
            f"/v1/schedules/{schedule_id}",
            json={"expected_revision": 1, "definition": _definition(EDITED_INSTRUCTION)},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["schedule"]["current_revision"] == 2
        assert edited.json()["schedule"]["state"] == "ACTIVE"
        assert edited.json()["revision"]["instruction"] == EDITED_INSTRUCTION

        # Run now is bound to the revision the owner saw.
        behind = await _run_now(client, schedule_id, "manual-behind", revision=1)
        assert behind.status_code == 409, behind.text
        assert _reason(behind) == "schedule.revision_conflict"
        started = await _run_now(client, schedule_id, "manual-1", revision=2)
        assert started.status_code == 201, started.text
        occurrence = started.json()
        assert occurrence["trigger"] == "MANUAL"
        assert occurrence["disposition"] == "MATERIALIZED"
        assert occurrence["schedule_revision"] == 2
        assert datetime.fromisoformat(occurrence["nominal_fire_at"]) == NOW
        run_id = UUID(occurrence["run_id"])
        replayed = await _run_now(client, schedule_id, "manual-1", revision=2)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["id"] == occurrence["id"]
        busy = await _run_now(client, schedule_id, "manual-2", revision=2)
        assert busy.status_code == 409, busy.text
        assert _reason(busy) == "schedule.run_active"

        queued = await client.get(f"/v1/runs/{run_id}")
        assert queued.json()["status"] == RunStatus.QUEUED.value
        assert await run_worker_once(app, "lifecycle-manual-1")
        finished = await client.get(f"/v1/runs/{run_id}")
        assert finished.json()["status"] == RunStatus.COMPLETED.value, finished.text
        transcript = await client.get(f"/v1/sessions/{occurrence['session_id']}/messages")
        assert transcript.status_code == 200, transcript.text
        texts = [
            part["text"]
            for message in transcript.json()["items"]
            for part in message["content"]
            if part.get("type") == "text"
        ]
        # The seed is durable model context, not a chat message: the transcript
        # shows only the reply, and the event log holds the edited instruction.
        assert texts == ["Two changes landed; one review is open."]
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(UUID(occurrence["session_id"]), 0, app.principal)
        seeds = [event for event in events if event.event_type == "user.message.created"]
        assert [seed.payload["content"] for seed in seeds] == [EDITED_INSTRUCTION]
        assert seeds[0].run_id == run_id
        after_run = await _schedule(client, schedule_id)
        assert after_run["schedule"]["next_fire_at"] == first_fire
        assert after_run["schedule"]["consecutive_failures"] == 0

        paused = await client.post(
            f"/v1/schedules/{schedule_id}/pause", json={"expected_revision": 2}
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["schedule"]["state"] == "PAUSED"
        assert paused.json()["schedule"]["pause_reason"] == "user"
        assert paused.json()["schedule"]["next_fire_at"] is None
        again = await client.post(
            f"/v1/schedules/{schedule_id}/pause", json={"expected_revision": 2}
        )
        assert again.status_code == 200 and again.json()["schedule"]["state"] == "PAUSED"

        # A paused schedule never fires on its cadence, but the owner may still run it.
        clock.advance(timedelta(days=1))
        schedule_worker = app.schedule_worker_factory()
        assert isinstance(schedule_worker, ScheduleWorker)
        assert await schedule_worker.run_once() == 0
        second = await _run_now(client, schedule_id, "manual-2", revision=2)
        assert second.status_code == 201, second.text
        assert await run_worker_once(app, "lifecycle-manual-2")
        second_run = await client.get(f"/v1/runs/{second.json()['run_id']}")
        assert second_run.json()["status"] == RunStatus.COMPLETED.value, second_run.text
        still_paused = await _schedule(client, schedule_id)
        assert still_paused["schedule"]["state"] == "PAUSED"
        assert still_paused["schedule"]["next_fire_at"] is None

        resumed = await client.post(
            f"/v1/schedules/{schedule_id}/resume", json={"expected_revision": 2}
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["schedule"]["state"] == "ACTIVE"
        assert resumed.json()["schedule"]["pause_reason"] is None
        # Strictly after the resume time, with no backfill of the paused day.
        next_fire = datetime.fromisoformat(resumed.json()["schedule"]["next_fire_at"])
        assert next_fire == datetime(2026, 10, 22, 9, tzinfo=UTC)
        assert await schedule_worker.run_once() == 0

        unscoped = Principal(
            tenant_id=app.principal.tenant_id,
            principal_id=app.principal.principal_id,
            roles={"user"},
            scopes={"schedule.read", "schedule.write", "schedule.run"},
        )
        async with api_client(app, principal=unscoped) as reader:
            forbidden = await reader.delete(
                f"/v1/schedules/{schedule_id}", params={"expected_revision": 2}
            )
            assert forbidden.status_code == 403, forbidden.text
        behind_delete = await client.delete(
            f"/v1/schedules/{schedule_id}", params={"expected_revision": 1}
        )
        assert behind_delete.status_code == 409, behind_delete.text
        assert (await _schedule(client, schedule_id))["schedule"]["state"] == "ACTIVE"

        stopped = await client.delete(
            f"/v1/schedules/{schedule_id}", params={"expected_revision": 2}
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["schedule"]["state"] == "CANCELLED"
        assert stopped.json()["schedule"]["next_fire_at"] is None
        repeated = await client.delete(
            f"/v1/schedules/{schedule_id}", params={"expected_revision": 2}
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["schedule"]["state"] == "CANCELLED"

        # Terminal: no run on request, no edit, no resume, and the cadence is dead.
        refused = await _run_now(client, schedule_id, "manual-3", revision=2)
        assert refused.status_code == 409 and _reason(refused) == "schedule.terminal"
        late_edit = await client.patch(
            f"/v1/schedules/{schedule_id}",
            json={"expected_revision": 2, "definition": _definition("Too late.")},
        )
        assert late_edit.status_code == 409, late_edit.text
        late_resume = await client.post(
            f"/v1/schedules/{schedule_id}/resume", json={"expected_revision": 2}
        )
        assert late_resume.status_code == 409, late_resume.text
        clock.advance(timedelta(days=2))
        assert await schedule_worker.run_once() == 0

    async with (
        build(settings=settings, storage="postgres", clock=clock) as restarted,
        api_client(restarted) as client,
    ):
        kept = await _schedule(client, schedule_id)
        assert kept["schedule"]["state"] == "CANCELLED"
        assert kept["schedule"]["current_revision"] == 2
        assert kept["revision"]["instruction"] == EDITED_INSTRUCTION
        history = await client.get(f"/v1/schedules/{schedule_id}/occurrences")
        assert history.status_code == 200, history.text
        rows = history.json()["items"]
        assert [row["trigger"] for row in rows] == ["MANUAL", "MANUAL"]
        assert {row["run_id"] for row in rows} == {str(run_id), second.json()["run_id"]}
        assert {row["schedule_revision"] for row in rows} == {2}
        active_only = await client.get("/v1/schedules", params={"state": "ACTIVE"})
        assert active_only.status_code == 200 and active_only.json()["items"] == []
        every = await client.get("/v1/schedules")
        assert [item["id"] for item in every.json()["items"]] == [str(schedule_id)]
