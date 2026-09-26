"""A device handoff verified and sealed by real browsers (ADR-0128 R-10, build plan R-14).

The isolated service runs as in production, with its encrypted filesystem
store and the real Playwright runtime, against a synthetic members-only site
served by the shared real-browser harness. The owner's device is played by
``httpx``: it signs in, then hands the session to the service.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from agent_core.browser_control_plane.api import create_profile_service_app
from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.runtime import HostedPlaywrightSessionRuntime
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserAuthenticationMode, BrowserAuthenticationStatus
from agent_core.domain.credentials import SecretValue
from tests.real_browser_support import (
    LocalHttpsSite,
    RealBrowserRuntime,
    local_https_site,
    require_real_browser,
)

PROFILE_ID = UUID("00000000-0000-4000-8000-0000000000d1")
RUN_ID = UUID("00000000-0000-4000-8000-0000000000d2")
PROVIDER_REF = "opaque-real-browser-reference-00000000001"
OWNER = Principal(tenant_id="tenant-a", principal_id="owner")
SERVICE_AUTH = "synthetic-real-browser-service-auth"
MEMBERS_TEXT = "Your lessons: Unit 1, Unit 2"


class MembersSite:
    """A public home page, a sign-in form, and a members-only lesson list."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(24)
        self.app = Starlette(
            routes=[
                Route("/", self.home),
                Route("/log-in", self.log_in, methods=["GET", "POST"]),
                Route("/learn", self.learn),
            ]
        )

    async def home(self, request: Request) -> Response:
        del request
        return HTMLResponse("<h1>Welcome</h1><a href='/log-in'>Sign in</a>")

    async def log_in(self, request: Request) -> Response:
        if request.method == "POST":
            response = RedirectResponse("/learn", status_code=303)
            response.set_cookie(
                "session", self.token, secure=True, httponly=True, samesite="lax", path="/"
            )
            return response
        return HTMLResponse(
            "<form method='post'><input name='email'>"
            "<input type='password' name='secret'><button>Sign in</button></form>"
        )

    async def learn(self, request: Request) -> Response:
        if request.cookies.get("session") != self.token:
            return RedirectResponse("/log-in", status_code=302)
        return HTMLResponse(f"<h1>{MEMBERS_TEXT}</h1>")


@dataclass
class ProfileService:
    site: LocalHttpsSite
    sessions: HostedProfileSessionService
    http: httpx.AsyncClient


@asynccontextmanager
async def profile_service(site: LocalHttpsSite, root: Path) -> AsyncIterator[ProfileService]:
    store = FilesystemEncryptedProfileStore(
        root,
        StaticProfileKeyring(
            {"key-v1": hashlib.sha256(b"synthetic-real-browser-key").digest()},
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
        process_secret=b"synthetic-real-browser-process-secret",
        ceremony_base_url="https://browser.service.test",
    )
    lifecycle = HostedProfileLifecycleService(
        store,
        reference_factory=lambda: PROVIDER_REF,
        invalidate_profile=sessions.invalidate_profile,
    )
    await lifecycle.provision(PROFILE_ID, OWNER, (site.origin,))
    app = create_profile_service_app(lifecycle, SecretValue(SERVICE_AUTH), sessions=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://browser.service.test"
    ) as http:
        yield ProfileService(site=site, sessions=sessions, http=http)


async def signed_in_cookie(site: MembersSite) -> dict[str, object]:
    """Sign in the way the owner's device does, and read the cookie it was given."""

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=site.app), base_url="https://site.test"
    ) as device:
        answer = await device.post("/log-in", data={"email": "owner", "secret": "synthetic"})
    assert answer.status_code == 303
    assert answer.cookies.get("session") == site.token
    return {
        "name": "session",
        "value": site.token,
        "domain": "site.test",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


async def hand_off(
    service: ProfileService, *, cookie: dict[str, object], confirmed_path: str
) -> tuple[httpx.Response, UUID]:
    ceremony = await service.sessions.begin_authentication(
        PROFILE_ID,
        OWNER,
        PROVIDER_REF,
        login_url=service.site.url("/"),
        mode=BrowserAuthenticationMode.DEVICE,
    )
    assert ceremony.launch_url is not None
    launch = urlsplit(ceremony.launch_url)
    response = await service.http.post(
        launch.path,
        headers={
            "X-Browser-Ceremony-Capability": parse_qs(launch.fragment)["capability"][0],
            "Content-Type": "application/json",
        },
        json={
            "confirmed_url": service.site.url(confirmed_path),
            "cookies": [cookie],
            "origins": [],
        },
    )
    return response, ceremony.id


async def test_a_handed_off_session_is_verified_sealed_and_used_by_a_lease(
    tmp_path: Path,
) -> None:
    require_real_browser()
    members = MembersSite()
    async with (
        local_https_site(members.app) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        response, ceremony_id = await hand_off(
            service, cookie=await signed_in_cookie(members), confirmed_path="/learn"
        )
        status = await service.sessions.authentication_status(ceremony_id, OWNER)
        lease = await service.sessions.acquire(
            PROFILE_ID,
            OWNER,
            PROVIDER_REF,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        try:
            lesson = await service.sessions.navigate(lease.lease_ref, site.url("/learn"))
        finally:
            await service.sessions.close(lease.lease_ref)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ready"}
    assert status.status is BrowserAuthenticationStatus.READY
    assert lesson.url == site.url("/learn")
    assert MEMBERS_TEXT in lesson.text
    assert set(site.relay.refused) == set()
    assert {policy.destinations[0].host for policy in site.egress_policies} == {"site.test"}


async def test_a_session_the_site_refuses_is_signed_out(tmp_path: Path) -> None:
    require_real_browser()
    members = MembersSite()
    async with (
        local_https_site(members.app) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        cookie = await signed_in_cookie(members)
        response, ceremony_id = await hand_off(
            service, cookie={**cookie, "value": "not-the-session"}, confirmed_path="/learn"
        )
        status = await service.sessions.authentication_status(ceremony_id, OWNER)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "session_signed_out"
    assert status.status is BrowserAuthenticationStatus.CANCELLED
    assert members.token not in response.text


async def test_a_page_anyone_can_see_does_not_confirm_a_sign_in(tmp_path: Path) -> None:
    require_real_browser()
    members = MembersSite()
    async with (
        local_https_site(members.app) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        response, ceremony_id = await hand_off(
            service, cookie=await signed_in_cookie(members), confirmed_path="/"
        )
        status = await service.sessions.authentication_status(ceremony_id, OWNER)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "session_unconfirmed"
    assert status.status is BrowserAuthenticationStatus.CANCELLED
