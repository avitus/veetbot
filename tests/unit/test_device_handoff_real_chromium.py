"""A device handoff verified and sealed by real browsers (ADR-0128 R-10, build plan R-14).

The isolated service runs as in production, with its encrypted filesystem
store and the real Playwright runtime, against a synthetic members-only site
served by the shared real-browser harness. The owner's device is played by
``httpx``: it signs in, then hands the session to the service.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_core.browser_control_plane.api import create_profile_service_app
from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.runtime import HostedPlaywrightSessionRuntime
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.browser_control_plane.verification import BrowserVerificationCatalog
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAuthenticationMode,
    BrowserAuthenticationStatus,
    BrowserInteractiveEvent,
    BrowserProviderError,
)
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
                Route("/late-form", self.late_form),
                Route("/moving", self.moving),
                Route("/moving/next", self.moving_next),
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

    async def late_form(self, request: Request) -> Response:
        """Asks everyone to sign in, session or not, once its requests have gone quiet."""
        del request
        return _late_form_page()

    async def moving(self, request: Request) -> Response:
        """Signed-out visitors are asked at once; a session is asked only on the next document."""
        if request.cookies.get("session") != self.token:
            return await self.log_in(request)
        return HTMLResponse(
            """<!doctype html><title>Loading</title><p>Loading</p><script>
            setTimeout(() => { location.href = '/moving/next'; }, 2500);
            </script>"""
        )

    async def moving_next(self, request: Request) -> Response:
        """Stays below the confirmed path and draws its form after the first page's time is up."""
        del request
        return HTMLResponse(
            """<!doctype html><title>Loading</title><p>Loading</p><script>
            setTimeout(() => {
                const frame = document.createElement('iframe');
                frame.srcdoc = '<input type="password">';
                document.body.appendChild(frame);
            }, 2500);
            </script>"""
        )

    async def learn(self, request: Request) -> Response:
        if request.cookies.get("session") != self.token:
            return RedirectResponse("/log-in", status_code=302)
        return HTMLResponse(f"<h1>{MEMBERS_TEXT}</h1>")


def _late_form_page() -> Response:
    return HTMLResponse(
        """<!doctype html><title>Loading</title><p>Loading</p><script>
        setTimeout(() => {
            const frame = document.createElement('iframe');
            frame.srcdoc = '<input type="password">';
            document.body.appendChild(frame);
        }, 3000);
        </script>"""
    )


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
    site: LocalHttpsSite,
    root: Path,
    *,
    verification_seconds: float = 30,
    verification: BrowserVerificationCatalog | None = None,
) -> AsyncIterator[ProfileService]:
    """Run an isolated real-browser profile service with optional site verification."""

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
        verification=verification,
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


class RestlessSignInSite:
    """Sign-in pages that keep moving as they load, as Chase's did on 2026-11-02."""

    FORM = "<form><input name='email'><input type='password' name='secret'></form>"

    def __init__(self) -> None:
        self.app = Starlette(
            routes=[
                Route("/routing", self.routing),
                Route("/replacing", self.replacing),
                Route("/settled", self.settled),
                Route("/account", self.account),
            ]
        )

    ROUTING = "<script>let n=0;setInterval(()=>{location.hash=String(++n)},2)</script>"

    async def routing(self, request: Request) -> Response:
        del request
        return HTMLResponse(self.FORM + self.ROUTING)

    async def account(self, request: Request) -> Response:
        """Signed in, the form gives way to a page that keeps routing."""
        if "session" in request.cookies:
            return HTMLResponse("<h1>Accounts</h1>" + self.ROUTING)
        response = HTMLResponse(
            "<form><input autofocus name='email'><input type='password' name='secret'></form>"
            "<script>document.querySelector('input').addEventListener("
            "'input',()=>location.assign('/account'))</script>"
        )
        response.set_cookie("session", "synthetic", secure=True)
        return response

    async def replacing(self, request: Request) -> Response:
        del request
        return HTMLResponse("<script>location.replace('/settled')</script>")

    async def settled(self, request: Request) -> Response:
        del request
        return HTMLResponse(self.FORM)


async def first_frame(service: ProfileService, ceremony_id: UUID, capability: str) -> bytes:
    """Fetch a frame as the owner's page does, which retries one Chromium fails to capture."""
    for _ in range(100):
        with suppress(PlaywrightError):
            return await service.sessions.authentication_frame(ceremony_id, capability)
        await asyncio.sleep(0.1)
    return await service.sessions.authentication_frame(ceremony_id, capability)


@pytest.mark.parametrize("path", ["/routing", "/replacing"])
async def test_a_remote_sign_in_opens_a_page_that_keeps_moving(tmp_path: Path, path: str) -> None:
    """The owner reads the page, so a page too busy for an agent to read still opens."""
    require_real_browser()
    async with (
        local_https_site(RestlessSignInSite().app) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        ceremony = await service.sessions.begin_authentication(
            PROFILE_ID, OWNER, PROVIDER_REF, login_url=site.url(path)
        )
        assert ceremony.launch_url is not None
        capability = parse_qs(urlsplit(ceremony.launch_url).fragment)["capability"][0]
        frame = await first_frame(service, ceremony.id, capability)
        await service.sessions.cancel_authentication(ceremony.id, OWNER)

    assert frame.startswith(b"\x89PNG")


async def test_a_remote_sign_in_finishes_on_a_page_that_keeps_moving(tmp_path: Path) -> None:
    require_real_browser()
    async with (
        local_https_site(RestlessSignInSite().app) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        ceremony = await service.sessions.begin_authentication(
            PROFILE_ID, OWNER, PROVIDER_REF, login_url=site.url("/account")
        )
        assert ceremony.launch_url is not None
        capability = parse_qs(urlsplit(ceremony.launch_url).fragment)["capability"][0]

        status = BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED
        for _ in range(100):
            refreshed = await service.sessions.refresh_authentication(ceremony.id, OWNER)
            status = refreshed.status
            if status is BrowserAuthenticationStatus.READY:
                break
            # A person types at the form and again if nothing happened. Chromium
            # focuses an autofocus field only when it draws, and a key sent sooner is lost.
            if status is BrowserAuthenticationStatus.NEEDS_USER:
                await service.sessions.authentication_event(
                    ceremony.id, capability, BrowserInteractiveEvent(kind="text", text="s")
                )
            await asyncio.sleep(0.1)

    assert status is BrowserAuthenticationStatus.READY


async def test_a_remote_sign_in_still_refuses_a_page_outside_the_profile(tmp_path: Path) -> None:
    require_real_browser()

    async def leaving(request: Request) -> Response:
        del request
        return RedirectResponse("https://elsewhere.test/", status_code=302)

    async with (
        local_https_site(Starlette(routes=[Route("/", leaving)])) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        with pytest.raises(BrowserProviderError) as refused:
            await service.sessions.begin_authentication(
                PROFILE_ID, OWNER, PROVIDER_REF, login_url=site.url("/")
            )

    assert refused.value.reason_code == "tool.browser.url_disallowed"


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


async def test_a_form_both_copies_draw_late_does_not_confirm_a_sign_in(tmp_path: Path) -> None:
    """ADR-0297: the control's wait must not turn one page into proof of a session."""
    require_real_browser()
    members = MembersSite()
    async with (
        local_https_site(members.app) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        response, ceremony_id = await hand_off(
            service, cookie=await signed_in_cookie(members), confirmed_path="/late-form"
        )
        status = await service.sessions.authentication_status(ceremony_id, OWNER)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "session_signed_out"
    assert status.status is BrowserAuthenticationStatus.CANCELLED


async def test_a_form_the_next_document_draws_late_does_not_confirm_a_sign_in(
    tmp_path: Path,
) -> None:
    """ADR-0297: the signed-in copy is watched from its newest document's load, not its first."""
    require_real_browser()
    members = MembersSite()
    async with (
        local_https_site(members.app) as site,
        profile_service(site, tmp_path / "profiles") as service,
    ):
        response, ceremony_id = await hand_off(
            service, cookie=await signed_in_cookie(members), confirmed_path="/moving"
        )
        status = await service.sessions.authentication_status(ceremony_id, OWNER)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "session_signed_out"
    assert status.status is BrowserAuthenticationStatus.CANCELLED


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
    tmp_path: Path, outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A rendered application shell is not yet the site's authentication decision."""
    require_real_browser()
    caplog.set_level(logging.INFO, logger="agent_core.browser_control_plane.sessions")
    # The overall deadline includes browser/page setup. Leave room for the
    # ten-second session-start bound plus the six-second pending assertion;
    # eight seconds could legitimately expire during that assertion under load.
    verification_seconds = 30
    checking_session = asyncio.Event()
    answer_session = asyncio.Event()
    session_answered = asyncio.Event()
    release_stream = asyncio.Event()

    class DelayedSessionSite(MembersSite):
        """Delay the authentication decision independently of the live updates stream."""

        async def learn(self, request: Request) -> Response:
            """Start live updates alongside the members-only page load."""
            response = await super().learn(request)
            if response.status_code != 200:
                return response
            if outcome == "timeout":
                # Exercise page setup that consumes part of the overall budget
                # before the test observes the pending session-check request.
                await asyncio.sleep(3)
            return HTMLResponse(
                "<main>Checking session</main><script>fetch('/updates');fetch('/session-check')"
                ".then(r => r.json()).then(result => {"
                "if (result.outcome === 'redirect') {location.href = '/log-in';}"
                "else if (result.outcome === 'challenge') {"
                "document.body.innerHTML = '<input type=\"password\">';}"
                "else {document.body.textContent = result.lessons;}"
                "});</script>"
            )

        async def updates(self, request: Request) -> Response:
            """Establish an event stream whose body stays open until test cleanup."""
            del request

            async def events() -> AsyncIterator[bytes]:
                """Flush an initial event-stream comment, then hold the connection open."""
                yield b": connected\n\n"
                await release_stream.wait()

            return StreamingResponse(events(), media_type="text/event-stream")

        async def check_session(self, request: Request) -> Response:
            """Return the delayed decision and rotate the session cookie on completion."""
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
    members.app.routes.append(Route("/updates", members.updates))
    async with (
        local_https_site(members.app) as site,
        profile_service(
            site, tmp_path / "profiles", verification_seconds=verification_seconds
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
            try:
                response, ceremony_id = await checking
            finally:
                release_stream.set()
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
            # The service says which load ran out the clock and where, and
            # nothing of the page or the session (ADR-0128 amendment).
            unfinished = [
                record
                for record in caplog.records
                if record.name == "agent_core.browser_control_plane.sessions"
            ]
            assert [record.msg for record in unfinished] == [
                "device verification did not finish (%s) after %.1f s"
            ]
            assert unfinished[0].args == (
                "with_session=settle",
                pytest.approx(verification_seconds, abs=2),
            )
            assert members.token not in repr(vars(unfinished[0]))
            assert "site.test" not in repr(vars(unfinished[0]))
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


async def test_an_open_event_stream_does_not_prevent_session_verification(tmp_path: Path) -> None:
    """A signed-in site's successful fetch stream stays open for live updates."""
    require_real_browser()
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()

    class StreamingSite(MembersSite):
        """Serve members-only content with an indefinitely open updates connection."""

        async def learn(self, request: Request) -> Response:
            """Start live updates alongside the members-only page load."""
            response = await super().learn(request)
            if response.status_code != 200:
                return response
            return HTMLResponse(
                bytes(response.body) + b"<script>fetch('/updates').then(r => r.text());</script>"
            )

        async def updates(self, request: Request) -> Response:
            """Establish an event stream whose body stays open until test cleanup."""
            assert request.cookies.get("session") == self.token

            async def events() -> AsyncIterator[bytes]:
                """Flush an initial event-stream comment, then hold the connection open."""
                yield b": connected\n\n"
                stream_started.set()
                await release_stream.wait()

            return StreamingResponse(events(), media_type="text/event-stream")

    members = StreamingSite()
    members.app.routes.append(Route("/updates", members.updates))
    async with (
        local_https_site(members.app) as site,
        profile_service(site, tmp_path / "profiles", verification_seconds=10) as service,
    ):
        try:
            response, ceremony_id = await hand_off(
                service, cookie=await signed_in_cookie(members), confirmed_path="/learn"
            )
            assert stream_started.is_set()
            assert not release_stream.is_set()
            assert response.status_code == 200, response.text
            assert (
                await service.sessions.authentication_status(ceremony_id, OWNER)
            ).status is BrowserAuthenticationStatus.READY
        finally:
            release_stream.set()
