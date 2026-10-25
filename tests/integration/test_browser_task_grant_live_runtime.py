"""A task grant's constraint refused by the live page, in real browsers (ADR-0129 R3).

The isolated service runs over HTTP as in production, with the real Playwright
runtime, against a synthetic lesson site served by the shared real-browser
harness (build plan N8). A covered click under a task-grant constraint is
dispatched; when the live page no longer matches what the worker was shown
(it moved to a settings path, a control was renamed to "Buy now", or a hidden
label says "Pay"), the act is refused before dispatch, the site records
nothing, and the next act reuses the same sequence number.

This case touches no database, so it replaces the directory's PostgreSQL reset.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from agent_core.browser_control_plane.api import create_profile_service_app
from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.runtime import HostedPlaywrightSessionRuntime
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue
from tests.real_browser_support import (
    LocalHttpsSite,
    RealBrowserRuntime,
    local_https_site,
    require_real_browser,
)

PROFILE_ID = UUID("00000000-0000-4000-8000-0000000000e1")
RUN_ID = UUID("00000000-0000-4000-8000-0000000000e2")
OWNER = Principal(tenant_id="tenant-a", principal_id="owner")
SERVICE_AUTH = "synthetic-live-grant-service-auth"


@pytest.fixture(autouse=True)
def isolate_postgres_case() -> None:
    """No database: this overrides the integration directory's PostgreSQL reset."""


def _page(body: str, *, script: str = "") -> HTMLResponse:
    record = "function record(name){fetch('/lesson/record',{method:'POST',body:name});}"
    return HTMLResponse(
        "<!doctype html><html><head><title>Lesson</title></head>"
        f"<body>{body}<script>{record}{script}</script></body></html>"
    )


@dataclass
class LessonSite:
    """Lesson pages whose controls report every click they receive."""

    clicks: list[str] = field(default_factory=list)
    changes_requested: dict[str, asyncio.Event] = field(
        default_factory=lambda: {name: asyncio.Event() for name in ("moving", "renaming")}
    )
    changes_completed: dict[str, asyncio.Event] = field(
        default_factory=lambda: {name: asyncio.Event() for name in ("moving", "renaming")}
    )

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/lesson/record", self.record, methods=["POST"]),
                Route("/lesson/change/{name}", self.wait_for_change),
                Route("/lesson/changed/{name}", self.changed, methods=["POST"]),
                Route("/lesson/start", self.start),
                Route("/lesson/moving", self.moving),
                Route("/lesson/renaming", self.renaming),
                Route("/lesson/hidden", self.hidden),
            ]
        )

    async def wait_for_change(self, request: Request) -> Response:
        await self.changes_requested[request.path_params["name"]].wait()
        return Response(status_code=204)

    async def changed(self, request: Request) -> Response:
        self.changes_completed[request.path_params["name"]].set()
        return Response(status_code=204)

    async def change_after_observation(self, name: str) -> None:
        """Require the page to acknowledge its mutation before dispatching an act."""
        self.changes_requested[name].set()
        async with asyncio.timeout(10):
            await self.changes_completed[name].wait()

    async def record(self, request: Request) -> Response:
        self.clicks.append((await request.body()).decode())
        return Response(status_code=204)

    async def start(self, request: Request) -> Response:
        del request
        return _page("<button onclick=\"record('start')\">Continue</button>")

    async def moving(self, request: Request) -> Response:
        del request
        return _page(
            "<button onclick=\"record('moving')\">Continue</button>",
            script=(
                "fetch('/lesson/change/moving').then(()=>{"
                "history.pushState({},'','/lesson/settings');"
                "return fetch('/lesson/changed/moving',{method:'POST'});});"
            ),
        )

    async def renaming(self, request: Request) -> Response:
        del request
        return _page(
            "<button id='b' onclick=\"record('renaming')\">Continue</button>",
            script=(
                "fetch('/lesson/change/renaming').then(()=>{"
                "document.getElementById('b').textContent='Buy now';"
                "return fetch('/lesson/changed/renaming',{method:'POST'});});"
            ),
        )

    async def hidden(self, request: Request) -> Response:
        del request
        return _page(
            "<button aria-label='Continue' onclick=\"record('hidden')\">Pay $12.99</button>"
            "<button onclick=\"record('approved')\">Next</button>"
        )


@dataclass
class LeasedService:
    site: LocalHttpsSite
    http: httpx.AsyncClient
    lease_ref: str
    grant_kind: str = "task"

    def _headers(self, operation: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {SERVICE_AUTH}"}
        if operation:
            digest = hashlib.sha256(self.lease_ref.encode()).hexdigest()[:24]
            headers["Idempotency-Key"] = f"browser-session:{digest}:{operation}"
        return headers

    async def navigate(self, path: str) -> dict[str, Any]:
        response = await self.http.post(
            "/v1/browser-sessions:navigate",
            headers=self._headers(""),
            json={"lease_ref": self.lease_ref, "url": self.site.url(path)},
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def observe(self) -> dict[str, Any]:
        response = await self.http.post(
            "/v1/browser-sessions:observe",
            headers=self._headers(""),
            json={"lease_ref": self.lease_ref},
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def click(
        self, page: dict[str, Any], name: str, *, sequence: int, constrained: bool
    ) -> httpx.Response:
        """Send a constrained click through the real isolated service lease."""
        element = next(item for item in page["elements"] if item["name"] == name)
        body: dict[str, Any] = {
            "lease_ref": self.lease_ref,
            "action": {
                "kind": "click",
                "expected_revision": page["revision"],
                "ref": element["ref"],
            },
            "sequence": sequence,
        }
        if constrained:
            body["constraint"] = {
                "grant_kind": self.grant_kind,
                "origins": [self.site.origin],
                "path_prefix": "/lesson" if self.grant_kind == "task" else None,
                "not_after": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                "consequence_ceiling": "unknown",
                "max_text_characters": 256,
            }
        return await self.http.post(
            "/v1/browser-sessions:act", headers=self._headers(f"act:{sequence}"), json=body
        )


@asynccontextmanager
async def leased_service(site: LocalHttpsSite, root: Path) -> AsyncIterator[LeasedService]:
    store = FilesystemEncryptedProfileStore(
        root,
        StaticProfileKeyring(
            {"key-v1": hashlib.sha256(b"synthetic-live-grant-key").digest()},
            current_version="key-v1",
        ),
    )
    sessions = HostedProfileSessionService(
        store,
        runtime_factory=lambda tenant_id: HostedPlaywrightSessionRuntime(
            tenant_id=tenant_id,
            runtime=RealBrowserRuntime(),
            proxy_factory=site.proxy_factory(),
        ),
        now=lambda: datetime.now(UTC),
        process_secret=b"synthetic-live-grant-process-secret",
        ceremony_base_url="https://browser.service.test",
    )
    lifecycle = HostedProfileLifecycleService(
        store,
        reference_factory=lambda: "opaque-live-grant-reference-000000000001",
        invalidate_profile=sessions.invalidate_profile,
    )
    app = create_profile_service_app(lifecycle, SecretValue(SERVICE_AUTH), sessions=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://browser.service.test"
    ) as http:
        headers = {"Authorization": f"Bearer {SERVICE_AUTH}"}
        provisioned = await http.post(
            "/v1/browser-profiles:provision",
            headers={**headers, "Idempotency-Key": f"browser-profile:{PROFILE_ID}:provision"},
            json={
                "profile_id": str(PROFILE_ID),
                "tenant_id": OWNER.tenant_id,
                "principal_id": OWNER.principal_id,
                "allowed_origins": [site.origin],
            },
        )
        assert provisioned.status_code == 201, provisioned.text
        acquired = await http.post(
            "/v1/browser-sessions:acquire",
            headers={
                **headers,
                "Idempotency-Key": f"browser-session:{PROFILE_ID}:{RUN_ID}:1:acquire",
            },
            json={
                "profile_id": str(PROFILE_ID),
                "tenant_id": OWNER.tenant_id,
                "principal_id": OWNER.principal_id,
                "provider_ref": provisioned.json()["provider_ref"],
                "run_id": str(RUN_ID),
                "attempt_number": 1,
                "deadline_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            },
        )
        assert acquired.status_code == 200, acquired.text
        lease_ref = acquired.json()["lease_ref"]
        try:
            yield LeasedService(site=site, http=http, lease_ref=lease_ref)
        finally:
            digest = hashlib.sha256(lease_ref.encode()).hexdigest()[:24]
            await http.post(
                "/v1/browser-sessions:close",
                headers={**headers, "Idempotency-Key": f"browser-session:{digest}:close"},
                json={"lease_ref": lease_ref},
            )


def _assert_refused(response: httpx.Response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["error"] == {
        "code": "tool.browser.grant_not_applicable",
        "message": "browser operation rejected",
    }
    assert set(response.json()) == {"error", "browser_diagnostics"}
    assert any(
        phase["failure"] == "grant_refused"
        for phase in response.json()["browser_diagnostics"]["phases"]
    )


@pytest.mark.parametrize("grant_kind", ["task", "research"])
async def test_the_live_page_decides_whether_a_task_grant_covers_an_act(
    tmp_path: Path, grant_kind: str
) -> None:
    """Both grant and research authority must reject changed live controls before effects."""
    require_real_browser()
    lessons = LessonSite()
    async with (
        local_https_site(lessons.app()) as site,
        leased_service(site, tmp_path / "profiles") as service,
    ):
        service.grant_kind = grant_kind
        start = await service.navigate("/lesson/start")
        covered = await service.click(start, "Continue", sequence=1, constrained=True)
        clicks_after_covered = list(lessons.clicks)

        moving = await service.navigate("/lesson/moving")
        await lessons.change_after_observation("moving")
        moved = await service.click(moving, "Continue", sequence=2, constrained=True)

        renaming = await service.navigate("/lesson/renaming")
        await lessons.change_after_observation("renaming")
        renamed = await service.click(renaming, "Continue", sequence=2, constrained=True)

        hidden = await service.navigate("/lesson/hidden")
        paid = await service.click(hidden, "Continue", sequence=2, constrained=True)
        stale = await service.click(hidden, "Next", sequence=2, constrained=False)
        clicks_after_refusals = list(lessons.clicks)

        fresh = await service.observe()
        approved = await service.click(fresh, "Next", sequence=2, constrained=False)
        await asyncio.sleep(0.2)
        clicks_at_end = list(lessons.clicks)

    assert covered.status_code == 200, covered.text
    assert clicks_after_covered == ["start"]
    assert moving["url"] == site.url("/lesson/moving")
    assert [element["name"] for element in renaming["elements"]] == ["Continue"]
    for response in (moved, renamed, paid):
        _assert_refused(response)
    # D16: a refusal forgets the observation, so its stale reference is page_changed.
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "tool.browser.page_changed"
    assert clicks_after_refusals == ["start"]
    # Every refusal left the sequence at 1: the next act is sequence 2.
    assert approved.status_code == 200, approved.text
    assert clicks_at_end == ["start", "approved"]
    assert site.relay.refused_page_targets() == set()
