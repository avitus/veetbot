"""Client-managed, server-persisted task approval scopes."""

from typing import Any

import httpx
import pytest

from tests.contract.test_browser_task_grant_api_contract import Stack, stack


@pytest.mark.asyncio
async def test_task_scopes_are_available_to_clients() -> None:
    system = await stack(scopes=())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=system.app()), base_url="http://test"
    ) as client:
        response = await client.get("/v1/browser-task-scopes")
    assert response.status_code == 200
    assert response.json() == {"revision": 0, "scopes": []}


SCOPE = {"origin": "https://www.example.org", "path_prefix": "/lesson"}
OTHER = {"origin": "https://www.example.net", "path_prefix": "/practice"}
PATH = "/v1/browser-task-scopes"


async def request(
    system: Stack, method: str = "GET", body: dict[str, Any] | None = None, **app_kwargs: Any
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=system.app(**app_kwargs), raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.request(method, PATH, json=body)


async def test_edits_are_shared_retryable_and_clear_is_durable() -> None:
    from tests.contract.support import principal
    from tests.contract.test_browser_task_grant_api_contract import SCOPES

    system = await stack()
    assert (await request(system)).json() == {"revision": 0, "scopes": [SCOPE]}
    update = {"revision": 0, "scopes": [SCOPE, OTHER]}
    saved = await request(system, "PUT", update)
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert (await request(system, "PUT", update)).json() == saved.json()
    assert (await request(system)).json() == saved.json()
    async with system.uow_factory() as uow:
        events = await uow.process_events.list("browser.task_scopes.updated")
        assert len(events) == 1
        assert events[0].payload["revision"] == 1
        assert events[0].actor_id == principal().principal_id
    stale = await request(system, "PUT", {"revision": 0, "scopes": []})
    assert stale.status_code == 409
    assert (await request(system, "PUT", {"revision": 1, "scopes": []})).json() == {
        "revision": 2,
        "scopes": [],
    }
    async with (
        system.uow_factory() as uow,
        uow.browser_task_grants.locked_scopes(principal(), defaults=SCOPES) as policy,
    ):
        assert policy.revision == 2 and policy.scopes == ()
    assert (await request(system)).json() == {"revision": 2, "scopes": []}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"scopes": []},
        {"revision": 0},
        {"revision": -1, "scopes": []},
        {"revision": True, "scopes": []},
        {"revision": 0, "scopes": [SCOPE, SCOPE]},
        {"revision": 0, "scopes": [{**SCOPE, "origin": "http://www.example.org"}]},
        {"revision": 0, "scopes": [{**SCOPE, "origin": "https://127.0.0.1"}]},
        {"revision": 0, "scopes": [{**SCOPE, "path_prefix": "/account"}]},
        {"revision": 0, "scopes": [{**SCOPE, "path_prefix": "/"}]},
        {"revision": 0, "scopes": [{**SCOPE, "path_prefix": "/lesson/next"}]},
        {"revision": 0, "scopes": [{**SCOPE, "path_prefix": "/lesson?x"}]},
        {"revision": 0, "scopes": [{**SCOPE, "path_prefix": f"/course{i}"} for i in range(17)]},
        {"revision": 0, "scopes": [], "principal_id": "someone-else"},
    ],
)
async def test_invalid_edits_leave_authority_unchanged(body: dict[str, Any]) -> None:
    system = await stack()
    response = await request(system, "PUT", body)
    assert response.status_code == 400
    assert (await request(system)).json() == {"revision": 0, "scopes": [SCOPE]}


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_feature_flag_hides_scope_routes(method: str) -> None:
    assert (
        await request(
            await stack(), method, {"revision": 0, "scopes": []}, browser_task_grants_enabled=False
        )
    ).status_code == 404


async def test_read_write_permissions_and_principal_isolation() -> None:
    system = await stack()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=system.app("browser.grant.read")), base_url="http://test"
    ) as client:
        assert (await client.get(PATH)).status_code == 200
        assert (await client.put(PATH, json={"revision": 0, "scopes": []})).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=system.app("browser.grant.write")), base_url="http://test"
    ) as client:
        assert (await client.get(PATH)).status_code == 403
    assert (await request(system, owner_update={"principal_id": "other"})).json() == {
        "revision": 0,
        "scopes": [],
    }
    assert (
        await request(
            system,
            "PUT",
            {"revision": 0, "scopes": [OTHER]},
            owner_update={"principal_id": "other"},
        )
    ).status_code == 200
    assert (await request(system)).json()["scopes"] == [SCOPE]


async def test_removal_rejects_stale_offer_and_ends_existing_grants_once() -> None:
    from tests.contract.test_browser_task_grant_api_contract import (
        ECHO,
        RESOLVE,
        post,
        resolved_stack,
    )

    system = await stack()
    assert (await request(system, "PUT", {"revision": 0, "scopes": []})).status_code == 200
    refused = await post(
        system.app(), RESOLVE, {"decision": "approve_for_task", "task_grant": ECHO}
    )
    assert refused.status_code == 409
    assert await system.grants() == []
    system, _ = await resolved_stack()
    update = {"revision": 0, "scopes": []}
    assert (await request(system, "PUT", update)).status_code == 200
    assert (await request(system, "PUT", update)).status_code == 200
    assert len(await system.events("browser.task_grant.ended")) == 1
    assert (await system.grants())[0].end_reason == "scope_removed"
    assert (await request(system, "PUT", {"revision": 1, "scopes": [SCOPE]})).status_code == 200
    assert (await system.grants())[0].end_reason == "scope_removed"
