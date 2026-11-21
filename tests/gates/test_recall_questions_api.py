"""ADR-0339 owner recall questions: the HTTP boundary.

Happy path, validation, authorization, failure and retry, through the composed
application: the routes exist only when the owner turns the feature on, each
needs its exact scope, a write names the revision it read, and no request can
move a question between the development and held-out halves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import httpx
from fastapi.routing import APIRoute

from agent_core.adapters.determinism import FixedClock
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from tests.contract.support import NOW
from tests.integration.m2_support import memory_settings

TENANT = "local"
BASE = "/v1/recall-questions"


def _principal(*scopes: str, principal_id: str = "local-user") -> Principal:
    return Principal(
        tenant_id=TENANT, principal_id=principal_id, roles={"user"}, scopes=set(scopes)
    )


@asynccontextmanager
async def _composition(*scopes: str, enabled: bool = True) -> AsyncIterator[Composition]:
    async with build(
        settings=replace(memory_settings(), recall_questions_enabled=enabled),
        storage="memory",
        sequential_ids=True,
        principal=_principal(*scopes),
        clock=FixedClock(NOW),
    ) as composition:
        yield composition


@asynccontextmanager
async def _client(
    composition: Composition, *, principal: Principal | None = None
) -> AsyncIterator[httpx.AsyncClient]:
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


def _content(fact: str = "Example Town", **overrides: Any) -> dict[str, Any]:
    return {
        "question": "Where do I live?",
        "family": "attributes",
        "expected": [{"statement_contains": fact}],
        **overrides,
    }


def _create(key: str = "home-city", split: str = "holdout", **content: Any) -> dict[str, Any]:
    return {"key": key, "split": split, "content": _content(**content)}


async def test_the_owner_adds_edits_retires_and_restores_a_question() -> None:
    async with (
        _composition("memory.read", "memory.write") as composition,
        _client(composition) as client,
    ):
        created = await client.post(BASE, json=_create())
        question_id = created.json()["id"]
        edited = await client.put(
            f"{BASE}/{question_id}",
            json={"expected_revision": 1, "content": _content("New Town", notes="moved")},
        )
        retired = await client.post(
            f"{BASE}/{question_id}/status", json={"expected_revision": 2, "status": "retired"}
        )
        active = await client.get(BASE, params={"status": "active"})
        held_out = await client.get(BASE, params={"split": "holdout"})
        restored = await client.post(
            f"{BASE}/{question_id}/status", json={"expected_revision": 3, "status": "active"}
        )
        history = await client.get(f"{BASE}/{question_id}/history")

    assert created.status_code == 201
    assert created.headers["cache-control"] == "private, no-store"
    assert created.json()["split"] == "holdout" and created.json()["revision"] == 1
    assert created.json()["latest_result"] is None and created.json()["passed"] is None
    assert edited.status_code == 200 and edited.json()["revision"] == 2
    assert edited.json()["content"]["expected"][0]["statement_contains"] == "New Town"
    assert retired.json()["status"] == "retired"
    assert active.json()["items"] == []
    assert [item["key"] for item in held_out.json()["items"]] == ["home-city"]
    assert restored.json()["status"] == "active" and restored.json()["revision"] == 4
    assert [(item["revision"], item["status"]) for item in history.json()["items"]] == [
        (1, "active"),
        (2, "active"),
        (3, "retired"),
        (4, "active"),
    ]
    # The owner sees a question, never the tenancy columns it is stored under.
    assert "tenant_id" not in created.json() and "principal_id" not in created.json()


async def test_no_request_moves_a_question_to_the_other_half() -> None:
    async with (
        _composition("memory.read", "memory.write") as composition,
        _client(composition) as client,
    ):
        created = await client.post(BASE, json=_create(split="holdout"))
        question_id = created.json()["id"]
        via_edit = await client.put(
            f"{BASE}/{question_id}",
            json={"expected_revision": 1, "content": _content(), "split": "development"},
        )
        via_content = await client.put(
            f"{BASE}/{question_id}",
            json={"expected_revision": 1, "content": _content(split="development")},
        )
        via_status = await client.post(
            f"{BASE}/{question_id}/status",
            json={"expected_revision": 1, "status": "active", "split": "development"},
        )
        via_recreate = await client.post(BASE, json=_create(split="development"))
        stored = await client.get(BASE)

    assert [via_edit.status_code, via_content.status_code, via_status.status_code] == [400] * 3
    assert via_recreate.status_code == 409
    assert [(item["split"], item["revision"]) for item in stored.json()["items"]] == [
        ("holdout", 1)
    ]


async def test_malformed_questions_are_refused_before_anything_is_stored() -> None:
    async with (
        _composition("memory.read", "memory.write") as composition,
        _client(composition) as client,
    ):
        refused = [
            await client.post(BASE, json=body)
            for body in (
                _create(key="Not A Key"),
                _create(split="both"),
                _create(family="hobbies"),
                # Nothing to reach and nothing to render: the question cannot pass or fail.
                {"key": "empty", "split": "holdout", "content": _content(expected=[])},
                _create(expected=[{"statement_contains": "A", "belief_id": "x"}]),
                {**_create(), "extra": 1},
            )
        ]
        listed = await client.get(BASE)
        bad_filter = await client.get(BASE, params={"split": "both"})
        bad_identifier = await client.get(f"{BASE}/not-a-uuid/history")

    assert [response.status_code for response in refused] == [400] * 6
    assert {response.json()["error"]["code"] for response in refused} == {"malformed_request"}
    assert listed.json()["items"] == []
    assert bad_filter.status_code == 400 and bad_identifier.status_code == 400


async def test_a_stale_write_conflicts_and_a_repeated_write_is_safe() -> None:
    async with (
        _composition("memory.read", "memory.write") as composition,
        _client(composition) as client,
    ):
        first = await client.post(BASE, json=_create())
        retry = await client.post(BASE, json=_create())
        different = await client.post(BASE, json=_create(fact="Another Town"))
        question_id = first.json()["id"]
        edit = {"expected_revision": 1, "content": _content("New Town")}
        saved = await client.put(f"{BASE}/{question_id}", json=edit)
        replayed = await client.put(f"{BASE}/{question_id}", json=edit)
        stale = await client.put(
            f"{BASE}/{question_id}",
            json={"expected_revision": 1, "content": _content("Third Town")},
        )
        stale_status = await client.post(
            f"{BASE}/{question_id}/status", json={"expected_revision": 1, "status": "retired"}
        )
        history = await client.get(f"{BASE}/{question_id}/history")

    # A retried create returns the question it already made; a different one conflicts.
    assert retry.status_code == 201 and retry.json()["id"] == question_id
    assert different.status_code == 409 and different.json()["error"]["code"] == "conflict"
    assert saved.json()["revision"] == 2
    # The retry of an edit that already landed is not a conflict and writes nothing.
    assert replayed.status_code == 200 and replayed.json()["revision"] == 2
    assert stale.status_code == 409 and stale_status.status_code == 409
    assert len(history.json()["items"]) == 2


async def test_each_route_requires_its_exact_scope() -> None:
    async with (
        _composition("memory.read", "memory.write") as composition,
        _client(composition) as client,
    ):
        question_id = (await client.post(BASE, json=_create())).json()["id"]
        async with _client(composition, principal=_principal("memory.read")) as reader:
            read = await reader.get(BASE)
            history = await reader.get(f"{BASE}/{question_id}/history")
            writes = [
                await reader.post(BASE, json=_create(key="second")),
                await reader.put(
                    f"{BASE}/{question_id}", json={"expected_revision": 1, "content": _content("X")}
                ),
                await reader.post(
                    f"{BASE}/{question_id}/status",
                    json={"expected_revision": 1, "status": "retired"},
                ),
            ]
        async with _client(composition, principal=_principal("memory.write")) as writer:
            unread = [await writer.get(BASE), await writer.get(f"{BASE}/{question_id}/history")]
        after = await client.get(BASE)

    assert read.status_code == 200 and history.status_code == 200
    assert [response.status_code for response in writes] == [403] * 3
    assert [response.status_code for response in unread] == [403] * 2
    assert [(item["key"], item["revision"]) for item in after.json()["items"]] == [("home-city", 1)]


async def test_another_owner_cannot_see_or_change_a_question() -> None:
    async with (
        _composition("memory.read", "memory.write") as composition,
        _client(composition) as client,
    ):
        question_id = (await client.post(BASE, json=_create())).json()["id"]
        stranger = _principal("memory.read", "memory.write", principal_id="someone-else")
        async with _client(composition, principal=stranger) as other:
            listed = await other.get(BASE)
            history = await other.get(f"{BASE}/{question_id}/history")
            edited = await other.put(
                f"{BASE}/{question_id}", json={"expected_revision": 1, "content": _content("X")}
            )
        mine = await client.get(BASE)

    assert listed.json()["items"] == []
    assert history.status_code == 404 and edited.status_code == 404
    assert history.json()["error"]["code"] == "not_found"
    assert mine.json()["items"][0]["revision"] == 1


async def test_the_routes_and_the_page_exist_only_when_the_owner_turns_them_on() -> None:
    async with (
        _composition("memory.read", "memory.write", enabled=False) as composition,
        _client(composition) as client,
    ):
        off = [
            await client.get(BASE),
            await client.post(BASE, json=_create()),
            await client.get("/recall-questions"),
        ]
    assert [response.status_code for response in off] == [404] * 3


async def test_the_page_shell_is_content_free_and_locked_down() -> None:
    async with _composition() as composition, _client(composition) as client:
        shell = await client.get("/recall-questions")
        script = await client.get("/recall-questions/app.js")
        style = await client.get("/recall-questions/style.css")
        data = await client.get(BASE)

    # The shell needs no scope because it holds nothing; the data behind it does.
    assert [shell.status_code, script.status_code, style.status_code] == [200] * 3
    assert data.status_code == 403
    policy = shell.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy and "'unsafe-inline'" not in policy
    assert shell.headers["cache-control"] == "private, no-store"
    assert "<script src=" in shell.text and "onclick" not in shell.text
    assert script.headers["content-type"].startswith("text/javascript")


async def test_the_router_exposes_exactly_its_routes_under_their_scopes() -> None:
    async with _composition("memory.read", "memory.write") as composition:
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
        if isinstance(route, APIRoute) and "recall-questions" in route.path
        for method in route.methods or set()
    }

    assert routes == {
        ("/recall-questions", "GET", None),
        ("/recall-questions/app.js", "GET", None),
        ("/recall-questions/style.css", "GET", None),
        (BASE, "GET", "memory.read"),
        (BASE, "POST", "memory.write"),
        (BASE + "/{question_id}", "PUT", "memory.write"),
        (BASE + "/{question_id}/status", "POST", "memory.write"),
        (BASE + "/{question_id}/history", "GET", "memory.read"),
    }
