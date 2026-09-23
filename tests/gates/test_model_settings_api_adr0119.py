"""ADR-0119 owner model settings: the HTTP boundary and its effect on new chats."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import MappingProxyType
from typing import Any
from uuid import UUID

import httpx
from fastapi.routing import APIRoute
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.messages import FakeModelScript, ReasoningEffort, ScriptedTurn
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import NOW
from tests.integration.m2_support import memory_settings

TENANT = "local"
PRINCIPAL_ID = "local-user"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
VIEW_FIELDS = {"version", "chat", "memory", "chat_options", "memory_options"}


def _principal(*scopes: str) -> Principal:
    return Principal(
        tenant_id=TENANT, principal_id=PRINCIPAL_ID, roles={"user"}, scopes=set(scopes)
    )


def _settings(*providers: str) -> Settings:
    return replace(
        memory_settings(),
        credentials=MappingProxyType({name: SecretStr("test-key") for name in providers}),
    )


@asynccontextmanager
async def _composition(
    *scopes: str, providers: tuple[str, ...] = ("openai", "anthropic")
) -> AsyncIterator[Composition]:
    async with build(
        settings=_settings(*providers),
        storage="memory",
        sequential_ids=True,
        model_policy="astra",
        principal=_principal(*scopes),
    ) as composition:
        yield composition


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


def _body(
    version: int,
    chat: tuple[str, str | None],
    memory: tuple[str, str | None] = ("balanced", None),
) -> dict[str, Any]:
    return {
        "expected_version": version,
        "chat": {"model_policy": chat[0], "reasoning_effort": chat[1]},
        "memory": {"model_policy": memory[0], "reasoning_effort": memory[1]},
    }


async def test_unsaved_settings_show_the_deployment_defaults_and_every_choice() -> None:
    async with (
        _composition("settings.read", "settings.write") as composition,
        _client(composition) as client,
    ):
        response = await client.get("/v1/settings/models")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    view = response.json()
    assert set(view) == VIEW_FIELDS
    assert view["version"] == 0
    assert view["chat"] == {"model_policy": "astra", "reasoning_effort": "high"}
    assert view["memory"] == {"model_policy": "balanced", "reasoning_effort": None}
    assert [option["model_policy"] for option in view["chat_options"]] == [
        "astra",
        "fable",
        "balanced",
    ]
    astra = view["chat_options"][0]
    assert astra == {
        "model_policy": "astra",
        "display_name": "GPT-6 Astra",
        "provider": "openai",
        "model": "gpt-6-astra",
        "reasoning_efforts": EFFORTS,
        "default_reasoning_effort": "high",
    }
    assert view["memory_options"] == [
        {
            "model_policy": "balanced",
            "display_name": "GPT-5.6 Sol",
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "reasoning_effort": None,
        }
    ]


async def test_a_model_whose_provider_has_no_credential_is_not_offered() -> None:
    async with (
        _composition("settings.read", providers=("openai",)) as composition,
        _client(composition) as client,
    ):
        view = (await client.get("/v1/settings/models")).json()

    assert [option["model_policy"] for option in view["chat_options"]] == ["astra", "balanced"]


async def test_a_save_versions_the_settings_and_records_a_content_free_event() -> None:
    async with _composition("settings.read", "settings.write") as composition:
        async with _client(composition) as client:
            saved = await client.put("/v1/settings/models", json=_body(0, ("fable", "xhigh")))
            reread = await client.get("/v1/settings/models")
        async with composition.uow_factory() as uow:
            events = await uow.process_events.list()

    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    assert saved.json()["chat"] == {"model_policy": "fable", "reasoning_effort": "xhigh"}
    assert reread.json() == saved.json()
    updated = [event for event in events if event.event_type == "settings.models.updated"]
    assert len(updated) == 1
    assert updated[0].payload == {
        "tenant_id": TENANT,
        "principal_id": PRINCIPAL_ID,
        "version": 1,
        "chat_model_policy": "fable",
        "chat_reasoning_effort": "xhigh",
        "memory_model_policy": "balanced",
        "memory_reasoning_effort": None,
    }


async def test_a_choice_that_is_not_offered_is_refused_without_a_write() -> None:
    refused = [
        _body(0, ("missing", "high")),
        _body(0, ("astra", None)),
        _body(0, ("astra", "none")),
        _body(0, ("astra", "high"), memory=("astra", "medium")),
        _body(0, ("astra", "high"), memory=("balanced", "low")),
        {**_body(0, ("astra", "high")), "unexpected": True},
        {"expected_version": 0, "chat": {"model_policy": "astra", "reasoning_effort": "high"}},
    ]
    async with (
        _composition("settings.read", "settings.write") as composition,
        _client(composition) as client,
    ):
        responses = [await client.put("/v1/settings/models", json=body) for body in refused]
        after = await client.get("/v1/settings/models")

    for response in responses:
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "malformed_request"
    assert after.json()["version"] == 0


async def test_a_stale_version_conflicts_but_a_repeated_save_is_harmless() -> None:
    async with (
        _composition("settings.read", "settings.write") as composition,
        _client(composition) as client,
    ):
        first = await client.put("/v1/settings/models", json=_body(0, ("fable", "high")))
        retried = await client.put("/v1/settings/models", json=_body(0, ("fable", "high")))
        stale = await client.put("/v1/settings/models", json=_body(0, ("astra", "low")))
        current = await client.put("/v1/settings/models", json=_body(1, ("astra", "low")))

    assert first.json()["version"] == 1
    assert retried.status_code == 200
    assert retried.json() == first.json()
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "conflict"
    assert current.json()["version"] == 2


async def test_each_route_requires_its_exact_scope() -> None:
    async with (
        _composition("settings.read") as composition,
        _client(composition) as client,
    ):
        read = await client.get("/v1/settings/models")
        write = await client.put("/v1/settings/models", json=_body(0, ("fable", "high")))
    async with _composition() as composition, _client(composition) as client:
        unread = await client.get("/v1/settings/models")

    assert read.status_code == 200
    assert write.status_code == 403
    assert unread.status_code == 403


async def test_the_router_exposes_exactly_two_routes_under_their_scopes() -> None:
    async with _composition("settings.read", "settings.write") as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
    flattened = [
        nested
        for route in app.routes
        for nested in (
            route.original_router.routes if hasattr(route, "original_router") else (route,)
        )
    ]
    routes = {
        (route.path, method, (route.openapi_extra or {}).get("required_scope"))
        for route in flattened
        if isinstance(route, APIRoute) and route.path.startswith("/v1/settings")
        for method in route.methods or set()
    }

    assert routes == {
        ("/v1/settings/models", "GET", "settings.read"),
        ("/v1/settings/models", "PUT", "settings.write"),
    }
    assert {"settings.read", "settings.write"} <= PLATFORM_SCOPES


async def test_new_app_chats_use_the_chosen_model_and_existing_chats_keep_theirs() -> None:
    async with (
        _composition(
            "settings.read", "settings.write", "session.write", "session.read"
        ) as composition,
        _client(composition) as client,
    ):
        before = await client.post("/v1/sessions", json={"agent_id": "general"})
        await client.put("/v1/settings/models", json=_body(0, ("fable", "high")))
        after = await client.post("/v1/sessions", json={"agent_id": "general"})
        await client.put("/v1/settings/models", json=_body(1, ("astra", "high")))
        restored = await client.post("/v1/sessions", json={"agent_id": "general"})
        async with composition.uow_factory() as uow:
            policies = []
            for response in (before, after, restored):
                session = await uow.sessions.get(UUID(response.json()["id"]), composition.principal)
                agent = await uow.agents.get_version(session.agent_id, session.agent_version)
                policies.append((session.agent_version, agent.model_policy))
            kept = await uow.sessions.get(UUID(before.json()["id"]), composition.principal)

    assert [policy for _, policy in policies] == ["astra", "fable", "astra"]
    assert policies[0][0] == policies[2][0]
    assert policies[1][0] != policies[0][0]
    assert kept.agent_version == policies[0][0]


async def test_every_run_sends_the_chosen_effort_from_its_next_message() -> None:
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="First."), ScriptedTurn(text="Second.")]),
        FixedClock(NOW),
    )
    async with build(
        settings=_settings("openai", "anthropic"),
        storage="memory",
        sequential_ids=True,
        model_policy="astra",
        principal=_principal("settings.read", "settings.write", "session.read"),
        model_provider_overrides={"openai": provider, "anthropic": provider},
    ) as composition:
        first = await composition.runs.submit("Hello.")
        session_id = (await composition.runs.wait_terminal(first)).session_id
        async with _client(composition) as client:
            saved = await client.put("/v1/settings/models", json=_body(0, ("astra", "low")))
        second = await composition.runs.submit("Again.", session_id)
        await composition.runs.wait_terminal(second)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)

    assert saved.status_code == 200
    assert [request.reasoning_effort for request in provider.requests] == [
        ReasoningEffort.HIGH,
        ReasoningEffort.LOW,
    ]
    started = [event for event in events if event.event_type == "model.request.started"]
    assert [event.payload["reasoning_effort"] for event in started] == ["high", "low"]
