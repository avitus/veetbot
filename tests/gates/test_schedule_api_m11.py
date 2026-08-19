"""Milestone 11 schedule HTTP boundary gates."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from fastapi.routing import APIRoute

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import OccurrenceDisposition, ScheduleOccurrence
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import ScheduleMaterializer
from tests.integration.m2_support import memory_settings

AGENT_ID = UUID("00000000-0000-0000-0000-000000000611")
NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)


def _agent() -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="Schedule API agent",
        instructions="Follow the scheduled instruction.",
        model_policy="fake",
        enabled_tools=[],
        policy_profile="default",
        limits=RunLimits(),
    )


def _definition(*, instruction: str = "Summarize project changes.") -> dict[str, object]:
    return {
        "title": "Daily briefing",
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
        "run_timeout_seconds": 60,
        "cadence": {
            "kind": "DAILY",
            "local_time": time(9).isoformat(),
            "timezone": "America/Los_Angeles",
        },
        "misfire_grace_seconds": 60,
        "max_consecutive_failures": 3,
    }


@asynccontextmanager
async def _client(composition: Composition, *, principal: Principal | None = None) -> Any:
    app = create_app(
        composition.services,
        composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def test_schedule_routes_cover_lifecycle_idempotency_pagination_and_scopes() -> None:
    principal = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )
    async with build(
        settings=replace(memory_settings(), schedule_api_enabled=True),
        storage="memory",
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        async with _client(composition) as client:
            missing_key = await client.post("/v1/schedules", json=_definition())
            assert missing_key.status_code == 400

            created = await client.post(
                "/v1/schedules",
                json=_definition(),
                headers={"Idempotency-Key": "daily-briefing"},
            )
            assert created.status_code == 201, created.text
            schedule_id = UUID(created.json()["schedule"]["id"])
            replay = await client.post(
                "/v1/schedules",
                json=_definition(),
                headers={"Idempotency-Key": "daily-briefing"},
            )
            assert replay.status_code == 200
            assert replay.json()["schedule"]["id"] == str(schedule_id)

            listing = await client.get("/v1/schedules", params={"limit": 1})
            assert listing.status_code == 200
            assert listing.json()["items"][0]["id"] == str(schedule_id)
            assert "instruction" not in listing.json()["items"][0]
            assert listing.json()["items"][0]["instruction_preview"]

            point = await client.get(f"/v1/schedules/{schedule_id}")
            assert point.status_code == 200
            assert point.json()["revision"]["instruction"] == "Summarize project changes."

            updated = await client.patch(
                f"/v1/schedules/{schedule_id}",
                json={"expected_revision": 1, "definition": _definition(instruction="Updated")},
            )
            assert updated.status_code == 200
            assert updated.json()["schedule"]["current_revision"] == 2

            paused = await client.post(
                f"/v1/schedules/{schedule_id}/pause", json={"expected_revision": 2}
            )
            assert paused.status_code == 200
            resumed = await client.post(
                f"/v1/schedules/{schedule_id}/resume", json={"expected_revision": 2}
            )
            assert resumed.status_code == 200

            occurrences = await client.get(
                f"/v1/schedules/{schedule_id}/occurrences", params={"limit": 1}
            )
            assert occurrences.status_code == 200
            assert occurrences.json() == {"items": [], "next_cursor": None}

            cancelled = await client.delete(
                f"/v1/schedules/{schedule_id}", params={"expected_revision": 2}
            )
            assert cancelled.status_code == 200

        reader = principal.model_copy(update={"scopes": {"schedule.read"}}, deep=True)
        async with _client(composition, principal=reader) as read_only:
            denied = await read_only.post(
                "/v1/schedules",
                json=_definition(),
                headers={"Idempotency-Key": "denied"},
            )
            assert denied.status_code == 403

        foreign = principal.model_copy(
            update={"tenant_id": "other", "principal_id": "other-user"}, deep=True
        )
        async with _client(composition, principal=foreign) as stranger:
            hidden = await stranger.get(f"/v1/schedules/{schedule_id}")
            assert hidden.status_code == 404

        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        all_routes = [
            nested
            for route in app.routes
            for nested in (
                route.original_router.routes if hasattr(route, "original_router") else (route,)
            )
        ]
        schedule_routes = [
            route
            for route in all_routes
            if isinstance(route, APIRoute) and route.path.startswith("/v1/schedules")
        ]
        assert len(schedule_routes) == 8
        assert {
            (method, (route.openapi_extra or {})["required_scope"])
            for route in schedule_routes
            for method in (route.methods or set())
        } >= {
            ("POST", "schedule.write"),
            ("GET", "schedule.read"),
            ("DELETE", "schedule.cancel"),
        }


async def test_offline_occurrence_history_paginates_every_disposition_and_run_link() -> None:
    principal = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )
    async with build(
        settings=replace(memory_settings(), schedule_api_enabled=True),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        async with _client(composition) as client:
            created = await client.post(
                "/v1/schedules",
                json=_definition(),
                headers={"Idempotency-Key": "offline-history"},
            )
            assert created.status_code == 201
            schedule_id = UUID(created.json()["schedule"]["id"])

            clock = composition.clock
            assert isinstance(clock, FixedClock)
            materializer = ScheduleMaterializer(
                uow_factory=composition.uow_factory,
                principals=StaticSchedulePrincipalDirectory(principal),
                admission=AllowScheduleAdmissionController(),
                clock=clock,
                ids=composition.ids,
                seed_checkpoint=DurableCheckpointSeeder(clock),
            )
            linked_occurrences: list[ScheduleOccurrence] = []
            for terminal_status in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            ):
                record = await composition.schedules.get(principal, schedule_id)
                assert record.schedule.next_fire_at is not None
                clock.advance(record.schedule.next_fire_at - clock.now())
                occurrence = await materializer.materialize(schedule_id)
                assert occurrence is not None and occurrence.run_id is not None
                linked_occurrences.append(occurrence)
                async with composition.uow_factory() as uow:
                    if terminal_status is RunStatus.CANCELLED:
                        await uow.runs.transition(
                            occurrence.run_id, RunStatus.QUEUED, terminal_status
                        )
                    else:
                        await uow.runs.transition(
                            occurrence.run_id, RunStatus.QUEUED, RunStatus.RUNNING
                        )
                        await uow.runs.transition(
                            occurrence.run_id, RunStatus.RUNNING, terminal_status
                        )

            other_dispositions = (
                OccurrenceDisposition.MISSED,
                OccurrenceDisposition.SKIPPED_OVERLAP,
                OccurrenceDisposition.AUTHORIZATION_FAILED,
                OccurrenceDisposition.CONFIGURATION_FAILED,
            )
            async with composition.uow_factory() as uow:
                for offset, disposition in enumerate(other_dispositions, start=1):
                    nominal = linked_occurrences[0].nominal_fire_at - timedelta(minutes=offset)
                    await uow.schedule_occurrences.insert(
                        ScheduleOccurrence(
                            id=composition.ids.new_id(),
                            schedule_id=schedule_id,
                            schedule_revision=1,
                            nominal_fire_at=nominal,
                            disposition=disposition,
                            reason_code=f"schedule.test_{disposition.value.lower()}",
                            created_at=clock.now(),
                        )
                    )

            recovered: list[dict[str, object]] = []
            cursor: str | None = None
            while True:
                response = await client.get(
                    f"/v1/schedules/{schedule_id}/occurrences",
                    params={"limit": 2, **({"cursor": cursor} if cursor else {})},
                )
                assert response.status_code == 200
                page = response.json()
                recovered.extend(page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break

            assert {item["disposition"] for item in recovered} == {
                disposition.value for disposition in OccurrenceDisposition
            }
            linked_run_ids = {item["run_id"] for item in recovered if item["run_id"] is not None}
            assert linked_run_ids == {str(occurrence.run_id) for occurrence in linked_occurrences}
            recovered_statuses: set[str] = set()
            for run_id in linked_run_ids:
                durable_run = await client.get(f"/v1/runs/{run_id}")
                assert durable_run.status_code == 200
                assert durable_run.json()["id"] == run_id
                recovered_statuses.add(durable_run.json()["status"])
            assert recovered_statuses == {
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            }


async def test_schedule_http_surface_is_absent_by_default() -> None:
    async with build(settings=memory_settings(), storage="memory") as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        assert not [
            route
            for route in app.routes
            if isinstance(route, APIRoute) and route.path.startswith("/v1/schedules")
        ]
