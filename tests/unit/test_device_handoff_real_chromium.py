"""A device handoff verified and sealed by real browsers (ADR-0128 R-10, build plan R-14).

The isolated service runs as in production, with its encrypted filesystem
store and the real Playwright runtime, against a synthetic members-only site
served by the shared real-browser harness. The owner's device is played by
``httpx``: it signs in, then hands the session to the service.
"""

from __future__ import annotations

import asyncio
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
import pytest
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

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


def refusing_headless_browsers(app: ASGIApp) -> ASGIApp:
    """Answer a browser that reports itself headless with an empty 403, as x.com did."""

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and "HeadlessChrome" in Headers(scope=scope).get(
            "user-agent", ""
        ):
            await Response(status_code=403)(scope, receive, send)
            return
        await app(scope, receive, send)

    return guarded


@dataclass
class ProfileService:
    site: LocalHttpsSite
    sessions: HostedProfileSessionService
    http: httpx.AsyncClient


@asynccontextmanager
async def profile_service(
    site: LocalHttpsSite, root: Path, *, verification_seconds: float = 30
) -> AsyncIterator[ProfileService]:
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
        verification_seconds=verification_seconds,
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
    assert site.relay.refused_page_targets() == set()
    assert {policy.destinations[0].host for policy in site.egress_policies} == {"site.test"}


async def test_a_site_that_refuses_headless_browsers_confirms_a_handed_off_session(
    tmp_path: Path,
) -> None:
    """Production, 2026-10-01: x.com answered the headless verification with 403 (ADR-0145)."""
    require_real_browser()
    members = MembersSite()
    async with (
        local_https_site(refusing_headless_browsers(members.app)) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        response, ceremony_id = await hand_off(
            service, cookie=await signed_in_cookie(members), confirmed_path="/learn"
        )
        status = await service.sessions.authentication_status(ceremony_id, OWNER)

    assert response.status_code == 200, response.text
    assert status.status is BrowserAuthenticationStatus.READY


async def test_a_lease_reads_a_site_that_refuses_headless_browsers(tmp_path: Path) -> None:
    require_real_browser()
    members = MembersSite()
    async with (
        local_https_site(refusing_headless_browsers(members.app)) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        lease = await service.sessions.acquire(
            PROFILE_ID,
            OWNER,
            PROVIDER_REF,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        try:
            home = await service.sessions.navigate(lease.lease_ref, site.url("/"))
        finally:
            await service.sessions.close(lease.lease_ref)

    assert "Welcome" in home.text


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


@pytest.mark.parametrize("outcome", ["ready", "signed_out", "challenge", "unconfirmed"])
@pytest.mark.parametrize("resource", ["image", "embedded_page"])
async def test_a_pending_image_does_not_prevent_the_site_from_verifying_the_session(
    tmp_path: Path, outcome: str, resource: str
) -> None:
    """The site's document can prove or reject sign-in while a passive resource waits."""
    require_real_browser()
    release_image = asyncio.Event()
    image_requested = asyncio.Event()
    embedded = (
        b'<img src="/pending.svg" alt="">'
        if resource == "image"
        else b'<iframe src="https://resource.test/frame"></iframe>'
    )

    class PendingImageSite(MembersSite):
        async def image(self, request: Request) -> Response:
            del request
            image_requested.set()
            await release_image.wait()
            return Response("", media_type="image/svg+xml")

        async def home(self, request: Request) -> Response:
            response = await super().home(request)
            background = (
                b"<script>fetch('/pending.svg').catch(() => {});</script>"
                if outcome != "unconfirmed"
                else b""
            )
            return HTMLResponse(bytes(response.body) + embedded + background)

        async def log_in(self, request: Request) -> Response:
            response = await super().log_in(request)
            if request.method == "POST":
                return response
            return HTMLResponse(bytes(response.body) + embedded)

        async def learn(self, request: Request) -> Response:
            response = await super().learn(request)
            if response.status_code != 200:
                # A signed-out landing page need not contain a password field
                # and may keep its own background fetch running indefinitely.
                return RedirectResponse("/", status_code=302)
            challenge = (
                b"<script>setTimeout(() => {const input = document.createElement('input');"
                b"input.type = 'password'; document.body.appendChild(input);}, 200);</script>"
                if outcome == "challenge"
                else b""
            )
            return HTMLResponse(bytes(response.body) + challenge + embedded)

        async def frame(self, request: Request) -> Response:
            del request
            return HTMLResponse('<h1>Embedded page</h1><img src="/pending.svg" alt="">')

    members = PendingImageSite()
    members.app.routes.append(Route("/pending.svg", members.image))
    embedded_app = Starlette(
        routes=[Route("/frame", members.frame), Route("/pending.svg", members.image)]
    )
    async with (
        local_https_site(members.app) as site,
        local_https_site(embedded_app, host="resource.test") as resources,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        site.relay.targets.update(resources.relay.targets)
        cookie = await signed_in_cookie(members)
        if outcome == "signed_out":
            cookie = {**cookie, "value": "not-the-session"}
        try:
            response, ceremony_id = await hand_off(
                service,
                cookie=cookie,
                confirmed_path="/" if outcome == "unconfirmed" else "/learn",
            )
            assert image_requested.is_set()
            assert not release_image.is_set()
        finally:
            release_image.set()
        status = await service.sessions.authentication_status(ceremony_id, OWNER)

        if outcome == "ready":
            assert response.status_code == 200, response.text
            assert status.status is BrowserAuthenticationStatus.READY
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
                assert MEMBERS_TEXT in lesson.text
                assert lesson.url == site.url("/learn")
            finally:
                await service.sessions.close(lease.lease_ref)
        else:
            assert response.status_code == 422, response.text
            code = "session_unconfirmed" if outcome == "unconfirmed" else "session_signed_out"
            assert response.json()["error"]["code"] == code
            assert status.status is BrowserAuthenticationStatus.CANCELLED


@pytest.mark.parametrize("outcome", ["ready", "challenge", "redirect", "timeout"])
async def test_verification_waits_for_the_sites_delayed_session_decision(
    tmp_path: Path, outcome: str
) -> None:
    """A rendered application shell is not yet the site's authentication decision."""
    require_real_browser()
    checking_session = asyncio.Event()
    answer_session = asyncio.Event()
    session_answered = asyncio.Event()

    class DelayedSessionSite(MembersSite):
        async def learn(self, request: Request) -> Response:
            response = await super().learn(request)
            if response.status_code != 200:
                return response
            return HTMLResponse(
                "<main>Checking session</main><script>fetch('/session-check')"
                ".then(r => r.json()).then(result => {"
                "if (result.outcome === 'redirect') {location.href = '/log-in';}"
                "else if (result.outcome === 'challenge') {"
                "document.body.innerHTML = '<input type=\"password\">';}"
                "else {document.body.textContent = result.lessons;}"
                "});</script>"
            )

        async def check_session(self, request: Request) -> Response:
            assert request.cookies.get("session") == self.token
            checking_session.set()
            await answer_session.wait()
            # A lease must receive the session produced by the verified page,
            # including this rotation, rather than the earlier device cookie.
            self.token = secrets.token_urlsafe(24)
            response = JSONResponse({"outcome": outcome, "lessons": MEMBERS_TEXT})
            response.set_cookie(
                "session", self.token, secure=True, httponly=True, samesite="lax", path="/"
            )
            session_answered.set()
            return response

    members = DelayedSessionSite()
    members.app.routes.append(Route("/session-check", members.check_session))
    async with (
        local_https_site(members.app) as site,
        profile_service(
            site, tmp_path / "profiles", verification_seconds=8 if outcome == "timeout" else 30
        ) as service,
    ):
        checking = asyncio.create_task(
            hand_off(service, cookie=await signed_in_cookie(members), confirmed_path="/learn")
        )
        try:
            async with asyncio.timeout(10):
                await checking_session.wait()
            completed, _ = await asyncio.wait({checking}, timeout=6)
            assert not completed, "The verifier decided before the site's session check finished"
            if outcome == "timeout":
                response, ceremony_id = await checking
        finally:
            answer_session.set()
            response, ceremony_id = await checking
        status = await service.sessions.authentication_status(ceremony_id, OWNER)
        if outcome == "ready":
            assert response.status_code == 200, response.text
            assert status.status is BrowserAuthenticationStatus.READY
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
                assert MEMBERS_TEXT in lesson.text
                assert lesson.url == site.url("/learn")
            finally:
                await service.sessions.close(lease.lease_ref)
        elif outcome == "timeout":
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "tool.browser.provider_unavailable"
            assert status.status is BrowserAuthenticationStatus.CANCELLED
            # Let the site's cancelled request finish rotating its cookie
            # before the owner signs in again and supplies a fresh session.
            async with asyncio.timeout(5):
                await session_answered.wait()
            retried, retry_id = await hand_off(
                service, cookie=await signed_in_cookie(members), confirmed_path="/learn"
            )
            assert retried.status_code == 200, retried.text
            assert (
                await service.sessions.authentication_status(retry_id, OWNER)
            ).status is BrowserAuthenticationStatus.READY
        else:
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "session_signed_out"
            assert status.status is BrowserAuthenticationStatus.CANCELLED
