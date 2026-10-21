"""A portal's root signs out, while its verified dashboard accepts the saved session."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from agent_core.browser_control_plane.verification import BrowserVerificationCatalog
from tests.real_browser_support import local_https_site, require_real_browser
from tests.unit.test_device_handoff_real_chromium import (
    OWNER,
    PROFILE_ID,
    PROVIDER_REF,
    RUN_ID,
    MembersSite,
    hand_off,
    profile_service,
    signed_in_cookie,
)


class PatientPortal(MembersSite):
    def __init__(self) -> None:
        super().__init__()
        self.root_visits = 0
        self.app.routes.append(Route("/MHO/Home", self.dashboard))

    async def home(self, request: Request) -> Response:
        del request
        self.root_visits += 1
        response = RedirectResponse("/log-in?action=logout", status_code=302)
        response.delete_cookie("session", secure=True, httponly=True)
        return response

    async def dashboard(self, request: Request) -> Response:
        if request.cookies.get("session") != self.token:
            return RedirectResponse("/log-in", status_code=302)
        return HTMLResponse(
            "<h1>Welcome, Fixture Patient!</h1><h2>Quick Actions</h2>"
            "<a href='/records'>View Test Results</a>"
        )


async def test_verified_dashboard_avoids_logout_across_fresh_browsers(tmp_path: Path) -> None:
    require_real_browser()
    portal = PatientPortal()
    async with local_https_site(portal.app) as site:
        catalog = BrowserVerificationCatalog.model_validate(
            {
                "sites": [
                    {
                        "id": "fixture-patient",
                        "version": 1,
                        "profile_id": str(PROFILE_ID),
                        "origin": site.origin,
                        "protected_path": "/MHO/Home",
                        "ready": {"kind": "text", "text": "Quick Actions"},
                        "account": {
                            "kind": "region",
                            "region_kind": "heading",
                            "text": "Welcome, Fixture Patient!",
                        },
                    }
                ]
            }
        )
        async with profile_service(site, tmp_path / "profiles", verification=catalog) as service:
            response, _ = await hand_off(
                service, cookie=await signed_in_cookie(portal), confirmed_path="/MHO/Home"
            )
            assert response.status_code == 200, response.text
            for attempt in (1, 2):
                lease = await service.sessions.acquire(
                    PROFILE_ID,
                    OWNER,
                    PROVIDER_REF,
                    run_id=RUN_ID,
                    attempt_number=attempt,
                    deadline_at=datetime.now(UTC) + timedelta(minutes=2),
                )
                try:
                    page = await service.sessions.navigate(lease.lease_ref, site.origin)
                    assert page.url == site.url("/MHO/Home")
                    assert "Welcome, Fixture Patient!" in page.text
                    assert page.interruption is None
                    repeated = await service.sessions.navigate(lease.lease_ref, site.origin)
                    assert repeated.url == site.url("/MHO/Home")
                    assert repeated.interruption is None
                finally:
                    await service.sessions.close(lease.lease_ref)
            assert portal.root_visits == 0
