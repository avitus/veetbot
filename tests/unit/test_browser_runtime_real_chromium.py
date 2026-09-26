"""The Playwright runtime on real lesson-like pages, in a real Chromium (ADR-0130).

Each page is served by the shared local HTTPS harness, so the production
runtime's origin guard, CDP document guard and egress routing all run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.domain.browser import BrowserProviderError
from tests import real_browser_support
from tests.real_browser_support import (
    REQUIRE_REAL_BROWSER,
    LocalHttpsSite,
    RealBrowserRuntime,
    local_https_site,
    require_real_browser,
)


def html(body: str, *, script: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><title>Lesson</title></head>"
        f"<body>{body}<script>{script}</script></body></html>"
    )


@asynccontextmanager
async def browsing(
    routes: list[Route],
) -> AsyncIterator[tuple[LocalHttpsSite, RealBrowserRuntime]]:
    require_real_browser()
    async with local_https_site(Starlette(routes=routes)) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,))
        try:
            yield site, runtime
        finally:
            await runtime.close()


def test_a_required_real_browser_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI skips these tests; a local run with the variable set proves they ran."""

    monkeypatch.setattr(real_browser_support, "chromium_installed", lambda: False)
    monkeypatch.setenv(REQUIRE_REAL_BROWSER, "1")
    with pytest.raises(pytest.fail.Exception):
        require_real_browser()
    monkeypatch.delenv(REQUIRE_REAL_BROWSER)
    with pytest.raises(pytest.skip.Exception):
        require_real_browser()


async def test_the_harness_serves_the_site_through_the_production_guards() -> None:
    """A document on the synthetic origin passes the runtime's guards; nothing escapes."""

    async def home(request: Request) -> Response:
        del request
        return html('<h1>Home</h1><img src="https://tracker.test/pixel.png" alt="">')

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.navigate("https://elsewhere.test/")

    assert observation.url == site.url("/")
    assert "Home" in observation.text
    assert refused.value.reason_code == "tool.browser.url_disallowed"
    assert f"{site.host}:443" in site.relay.tunnelled
    assert "elsewhere.test:443" not in site.relay.tunnelled
    assert set(site.relay.refused) <= {"tracker.test:443"}


async def test_the_production_runtime_still_refuses_the_harness_certificate() -> None:
    """Only the test subclass trusts the throwaway certificate."""

    require_real_browser()

    async def home(request: Request) -> Response:
        del request
        return html("<h1>Home</h1>")

    async with local_https_site(Starlette(routes=[Route("/", home)])) as site:
        runtime = PythonPlaywrightRuntime()
        await runtime.start(site.proxy_url, (site.origin,))
        try:
            with pytest.raises(BrowserProviderError) as raised:
                await runtime.navigate(site.url("/"))
        finally:
            await runtime.close()

    assert raised.value.reason_code == "tool.browser.provider_unavailable"
