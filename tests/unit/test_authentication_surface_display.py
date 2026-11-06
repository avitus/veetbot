"""The direct authentication surface shows the website at actual size and keeps it current."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.async_api import Browser, Page, Route, async_playwright

from agent_core.browser_control_plane.api import _AUTHENTICATION_HTML, _AUTHENTICATION_SCRIPT

ORIGIN = "https://profiles.surface.test"
CEREMONY_PATH = "/authentication/00000000-0000-4000-8000-000000000002"
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720


def _chromium_installed() -> bool:
    async def probe() -> str:
        async with async_playwright() as playwright:
            return playwright.chromium.executable_path

    try:
        return Path(asyncio.run(probe())).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_installed(), reason="Playwright Chromium is not installed"
)


@dataclass
class SurfaceTraffic:
    """What the surface asked of the isolated browser service."""

    events: list[dict[str, object]] = field(default_factory=list)
    frames: int = 0
    frames_in_flight: int = 0
    most_frames_in_flight: int = 0


Opener = Callable[..., Awaitable[tuple[Page, SurfaceTraffic]]]


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch()
        try:
            yield launched
        finally:
            await launched.close()


@pytest.fixture
async def frame_image(browser: Browser) -> bytes:
    """A website-sized frame, like the 1280 by 720 page the remote browser draws."""
    page = await browser.new_page(viewport={"width": FRAME_WIDTH, "height": FRAME_HEIGHT})
    await page.set_content("<body style='margin:0;background:#cde'></body>")
    image = await page.screenshot(type="jpeg")
    await page.close()
    return image


@pytest.fixture
async def open_surface(browser: Browser, frame_image: bytes) -> AsyncIterator[Opener]:
    """Open the surface in a window of the given size, serving each frame after a delay."""
    pages: list[Page] = []

    async def opener(
        *, width: int, height: int, frame_delay: float = 0.0
    ) -> tuple[Page, SurfaceTraffic]:
        traffic = SurfaceTraffic()

        async def serve(route: Route) -> None:
            request = route.request
            path = request.url.removeprefix(ORIGIN).split("#", 1)[0]
            if path == CEREMONY_PATH:
                await route.fulfill(content_type="text/html", body=_AUTHENTICATION_HTML)
            elif path == "/authentication-surface.js":
                await route.fulfill(content_type="text/javascript", body=_AUTHENTICATION_SCRIPT)
            elif path == CEREMONY_PATH + "/frame":
                traffic.frames += 1
                traffic.frames_in_flight += 1
                traffic.most_frames_in_flight = max(
                    traffic.most_frames_in_flight, traffic.frames_in_flight
                )
                try:
                    await asyncio.sleep(frame_delay)
                    await route.fulfill(content_type="image/jpeg", body=frame_image)
                finally:
                    traffic.frames_in_flight -= 1
            elif path == CEREMONY_PATH + "/events" and request.method == "POST":
                traffic.events.append(json.loads(request.post_data or "{}"))
                await route.fulfill(status=204)
            else:
                await route.fulfill(status=404)

        page = await browser.new_page(viewport={"width": width, "height": height})
        pages.append(page)
        await page.route(f"{ORIGIN}/**", serve)
        await page.goto(f"{ORIGIN}{CEREMONY_PATH}#capability=synthetic-capability")
        await page.wait_for_function("document.getElementById('frame').naturalWidth > 0")
        return page, traffic

    yield opener
    for page in pages:
        # Let a frame still being served finish before the browser goes away.
        await page.unroute_all(behavior="wait")
        await page.close()


async def test_a_laptop_window_shows_the_website_at_actual_size(open_surface: Opener) -> None:
    """Shrinking the page to fit beside the instructions made sign-in fields unreadably small."""
    page, _ = await open_surface(width=1440, height=900)

    box = await page.locator("#frame").bounding_box()

    assert box is not None
    assert box["width"] == FRAME_WIDTH
    assert box["height"] == FRAME_HEIGHT


async def test_a_phone_keeps_actual_size_and_a_click_after_scrolling_lands_where_seen(
    open_surface: Opener,
) -> None:
    """A narrow window scrolls the website rather than shrinking it to a quarter of its size."""
    page, traffic = await open_surface(width=390, height=844)
    wrap = page.locator("#frame-wrap")
    await wrap.evaluate("wrap => { wrap.scrollLeft = 600; }")

    box = await page.locator("#frame").bounding_box()
    scrolled = await wrap.evaluate("wrap => wrap.scrollLeft")
    await page.locator("#frame").click(position={"x": 700, "y": 300})
    for _ in range(100):
        if traffic.events:
            break
        await page.wait_for_timeout(20)

    assert box is not None
    assert box["width"] == FRAME_WIDTH
    assert scrolled > 0
    assert traffic.events[0] == {"kind": "click", "x": 700, "y": 300}


async def test_the_website_redraws_several_times_a_second_while_the_user_is_typing(
    open_surface: Opener,
) -> None:
    """A once-a-second picture made every key and page change feel late."""
    page, traffic = await open_surface(width=1440, height=900)
    await page.locator("#frame").click(position={"x": 10, "y": 10})
    before = traffic.frames

    await page.wait_for_timeout(1000)

    assert traffic.frames - before >= 3


async def test_a_slow_frame_is_never_requested_twice_at_once(open_surface: Opener) -> None:
    """Overlapping frame requests would queue behind keys for the same browser."""
    page, traffic = await open_surface(width=1440, height=900, frame_delay=0.3)
    await page.locator("#frame").click(position={"x": 10, "y": 10})
    await page.keyboard.type("abc")

    await page.wait_for_timeout(1500)

    assert traffic.most_frames_in_flight == 1
    assert [event["kind"] for event in traffic.events] == ["click", "text", "text", "text"]
