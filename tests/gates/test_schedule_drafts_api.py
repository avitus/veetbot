"""ADR-0225 part B at the HTTP boundary: POST /v1/schedules/drafts.

Happy path through to creation, validation, authorization, an unavailable
interpreter, and the hourly allowance; nothing is stored by a draft.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.messages import FakeModelScript, ModelUsage, ScriptedTurn
from agent_core.domain.runs import RunLimits
from agent_core.domain.schedules import ScheduleDefinitionLimits
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.scheduling.drafts import (
    DRAFTS_PER_HOUR,
    ScheduleDrafter,
    ScheduleDraftInterpreter,
)
from tests.integration.m2_support import memory_settings
from tests.unit.test_schedule_drafts import NOW, _answer, _Router

AGENT = AgentSpec(
    id=UUID("00000000-0000-0000-0000-000000000711"),
    version="1.0.0",
    name="Draft API agent",
    instructions="Follow the scheduled instruction.",
    model_policy="fake",
    enabled_tools=[],
    policy_profile="default",
    limits=RunLimits(max_steps=8, max_model_calls=8, max_tool_calls=8, max_cost=Decimal("2")),
)
LIMITS = ScheduleDefinitionLimits(
    max_run_timeout_seconds=900,
    max_misfire_grace_seconds=7200,
    max_steps_per_run=30,
    max_model_calls_per_run=30,
    max_tool_calls_per_run=60,
    max_cost_per_run=Decimal("5"),
)
OWNER = Principal(
    tenant_id="local", principal_id="local-user", roles={"user"}, scopes=set(PLATFORM_SCOPES)
)
BODY = {
    "text": "Every weekday at 7 am, tell me what needs me today.",
    "timezone": "America/Los_Angeles",
}


@asynccontextmanager
async def _client(
    *answers: str, principal: Principal = OWNER, with_drafter: bool = True
) -> AsyncIterator[tuple[httpx.AsyncClient, Composition]]:
    async with build(
        settings=replace(memory_settings(), schedule_api_enabled=True),
        storage="memory",
        sequential_ids=True,
        principal=OWNER,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(AGENT)
        provider = FakeModelProvider(
            FakeModelScript(
                turns=[
                    ScriptedTurn(text=answer, usage=ModelUsage(input_tokens=200, output_tokens=40))
                    for answer in answers
                ]
            ),
            FixedClock(NOW),
        )
        ids = SequenceIdFactory()
        drafter = ScheduleDrafter(
            uow_factory=composition.uow_factory,
            clock=FixedClock(NOW),
            ids=ids,
            agent=AGENT,
            limits=LIMITS,
            interpreter=ScheduleDraftInterpreter(
                router=_Router(),
                providers={"openai": provider},
                clock=FixedClock(NOW),
                ids=ids,
                model_policy="balanced",
            ),
        )
        services = replace(composition.services, schedule_drafts=drafter if with_drafter else None)
        app = create_app(
            services,
            composition.settings,
            principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            yield client, composition


async def test_a_draft_can_be_posted_as_the_schedule_it_describes() -> None:
    async with _client(_answer()) as (client, composition):
        drafted = await client.post("/v1/schedules/drafts", json=BODY)
        assert drafted.status_code == 200, drafted.text
        assert drafted.headers["cache-control"] == "private, no-store"
        draft = drafted.json()
        assert draft["status"] == "ready"
        assert draft["definition"]["instruction"] == BODY["text"]
        assert draft["definition"]["agent_id"] == str(AGENT.id)
        assert len(draft["next_fire_at"]) == 3
        async with composition.uow_factory() as uow:
            listed = await uow.schedules.list(OWNER, limit=10)
        assert listed == [], "a draft stores nothing"

        created = await client.post(
            "/v1/schedules",
            json=draft["definition"],
            headers={"Idempotency-Key": "draft-1"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["revision"]["cadence"]["weekdays"] == [1, 2, 3, 4, 5]


async def test_an_unknown_time_zone_or_extra_field_is_refused() -> None:
    async with _client(_answer()) as (client, _):
        zone = await client.post("/v1/schedules/drafts", json={**BODY, "timezone": "Mars/Olympus"})
        assert zone.status_code == 422
        assert zone.json()["error"]["details"]["reason"] == "schedule.timezone_invalid"
        extra = await client.post("/v1/schedules/drafts", json={**BODY, "save": True})
        assert extra.status_code in {400, 422}
        empty = await client.post("/v1/schedules/drafts", json={**BODY, "text": ""})
        assert empty.status_code in {400, 422}


async def test_drafting_needs_schedule_write() -> None:
    reader = Principal(
        tenant_id="local", principal_id="local-user", roles={"user"}, scopes={"schedule.read"}
    )
    async with _client(_answer(), principal=reader) as (client, _):
        denied = await client.post("/v1/schedules/drafts", json=BODY)
        assert denied.status_code == 403


async def test_a_server_without_an_interpreter_says_so() -> None:
    async with _client(_answer(), with_drafter=False) as (client, _):
        unavailable = await client.post("/v1/schedules/drafts", json=BODY)
        assert unavailable.status_code == 422
        assert unavailable.json()["error"]["details"]["reason"] == "schedule.drafts_unavailable"


async def test_the_hourly_allowance_answers_429_with_its_reason() -> None:
    async with _client(*[_answer()] * DRAFTS_PER_HOUR) as (client, _):
        for _ in range(DRAFTS_PER_HOUR):
            assert (await client.post("/v1/schedules/drafts", json=BODY)).status_code == 200
        limited = await client.post("/v1/schedules/drafts", json=BODY)
        assert limited.status_code == 429
        body: dict[str, Any] = limited.json()
        assert body["error"]["code"] == "rate_limited"
        assert body["error"]["details"]["reason"] == "schedule.drafts_rate_limited"
        assert json.dumps(body).count(BODY["text"]) == 0
