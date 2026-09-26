"""The Playwright runtime on real lesson-like pages, in a real Chromium (ADR-0130).

Each page is served by the shared local HTTPS harness, so the production
runtime's origin guard, CDP document guard and egress routing all run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserElement,
    BrowserObservation,
    BrowserProviderError,
)
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


async def _exercise_api(request: Request) -> Response:
    del request
    await asyncio.sleep(0.3)
    return JSONResponse({"title": "Exercise 1"})


async def _late_page(request: Request) -> Response:
    del request
    return html(
        '<main id="main">Loading</main>',
        script=(
            "fetch('/api/exercise').then(r=>r.json()).then(d=>{"
            "document.getElementById('main').innerHTML="
            "'<h1>'+d.title+'</h1><button>Check</button>';});"
        ),
    )


async def _exercise_page(request: Request) -> Response:
    del request
    return html(
        '<main id="main"><h1>Exercise 1</h1><button id="check">Check</button></main>',
        script=(
            "document.getElementById('check').addEventListener('click',()=>{"
            "const m=document.getElementById('main');m.textContent='Checking';"
            "setTimeout(()=>{m.innerHTML='<p>Correct!</p><button id=\"next\">Continue</button>';"
            "document.getElementById('next').addEventListener('click',()=>{"
            "m.innerHTML='<h1>Exercise 2</h1>';});},250);});"
        ),
    )


async def _crowded_page(request: Request) -> Response:
    del request
    hidden = "".join(
        f'<button style="display:none">Hidden {index}</button>' for index in range(300)
    )
    visible = "".join(f"<button>Answer {index}</button>" for index in range(5))
    return html(hidden + visible)


async def _contents_page(request: Request) -> Response:
    del request
    return html(
        '<div role="button" style="display:contents"><span>Contents choice</span></div>'
        '<a href="/x" style="display:contents"><span>Contents link</span></a>'
        '<div role="button" style="display:contents"></div>'
        '<button style="visibility:collapse">Collapsed</button>'
        "<button>Plain</button>"
    )


async def _collapsed_page(request: Request) -> Response:
    del request
    collapsed = "".join(
        f'<button style="visibility:collapse">Collapsed {index}</button>' for index in range(300)
    )
    return html(collapsed + "<button>Answer</button>")


async def _restless_page(request: Request) -> Response:
    del request
    return html(
        '<p id="clock">0</p><button>Stay</button>',
        script=(
            "let n=0;setInterval(()=>{"
            "document.getElementById('clock').textContent=String(++n);},100);"
        ),
    )


async def _linking_page(request: Request) -> Response:
    del request
    return html('<h1>Page one</h1><a href="/two">Next</a>')


async def _second_document(request: Request) -> Response:
    del request
    await asyncio.sleep(0.3)
    return html("<h1>Page two</h1><button>Onward</button>")


LESSON_ROUTES = [
    Route("/late", _late_page),
    Route("/api/exercise", _exercise_api),
    Route("/exercise", _exercise_page),
    Route("/crowded", _crowded_page),
    Route("/contents", _contents_page),
    Route("/collapsed", _collapsed_page),
    Route("/restless", _restless_page),
    Route("/one", _linking_page),
    Route("/two", _second_document),
]


def _element(observation: BrowserObservation, name: str) -> BrowserElement:
    return next(element for element in observation.elements if element.name == name)


def _click(observation: BrowserObservation, name: str) -> BrowserAction:
    return BrowserAction(
        kind=BrowserActionKind.CLICK,
        expected_revision=observation.revision,
        ref=_element(observation, name).ref,
    )


async def test_navigation_waits_for_the_exercise() -> None:
    """ADR-0130 decision 5: navigate returns the page once it settles."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        observation = await runtime.navigate(site.url("/late"))

    assert "Exercise 1" in observation.text
    assert "Loading" not in observation.text
    assert [element.name for element in observation.elements] == ["Check"]


async def test_hidden_controls_never_take_an_element_slot_in_chromium() -> None:
    """ADR-0130 decision 8: the 256 slots go to visible elements, in document order."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        observation = await runtime.navigate(site.url("/crowded"))

    assert [element.name for element in observation.elements] == [
        f"Answer {index}" for index in range(5)
    ]


async def test_the_visibility_scan_keeps_display_contents_controls() -> None:
    """The one-call scan matches Playwright's is_visible (review finding): a
    display:contents control is visible when one of its children is."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        contents = await runtime.navigate(site.url("/contents"))

    assert [(element.role, element.name) for element in contents.elements] == [
        ("button", "Contents choice"),
        ("link", "Contents link"),
        ("button", "Plain"),
    ]


async def test_collapsed_controls_never_take_an_element_slot() -> None:
    """visibility:collapse is hidden to is_visible, so the scan drops it first."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        collapsed = await runtime.navigate(site.url("/collapsed"))

    assert [element.name for element in collapsed.elements] == ["Answer"]


async def test_action_returns_the_page_after_it_settles() -> None:
    """ADR-0130 decision 6: act returns the settled page, not the first repaint."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        page = await runtime.navigate(site.url("/exercise"))
        checked = await runtime.act(_click(page, "Check"))

    assert "Correct!" in checked.text
    assert "Checking" not in checked.text
    assert checked.revision != page.revision


async def test_act_can_follow_act_on_the_returned_revision() -> None:
    """The returned revision and refs are live: no observe between two acts."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        page = await runtime.navigate(site.url("/exercise"))
        checked = await runtime.act(_click(page, "Check"))
        continued = await runtime.act(_click(checked, "Continue"))

    assert "Exercise 2" in continued.text


async def test_a_page_that_never_settles_is_observed_at_the_bound() -> None:
    async with browsing(LESSON_ROUTES) as (site, runtime):
        started = asyncio.get_running_loop().time()
        observation = await runtime.navigate(site.url("/restless"))
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 3
    assert [element.name for element in observation.elements] == ["Stay"]


async def test_a_link_that_loads_a_new_document_settles_on_it() -> None:
    async with browsing(LESSON_ROUTES) as (site, runtime):
        page = await runtime.navigate(site.url("/one"))
        followed = await runtime.act(_click(page, "Next"))

    assert followed.url == site.url("/two")
    assert "Page two" in followed.text
    assert [element.name for element in followed.elements] == ["Onward"]
