"""The direct authentication surface relays each key live, run in a real Chromium."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import Page, Route, async_playwright

from agent_core.browser_control_plane.api import _AUTHENTICATION_HTML, _AUTHENTICATION_SCRIPT

ORIGIN = "https://profiles.surface.test"
CEREMONY_PATH = "/authentication/00000000-0000-4000-8000-000000000001"
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


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


@pytest.fixture
async def surface() -> AsyncIterator[tuple[Page, list[dict[str, object]]]]:
    """Open the surface with its capability, a one-pixel frame, and recorded events."""
    events: list[dict[str, object]] = []

    async def serve(route: Route) -> None:
        request = route.request
        path = request.url.removeprefix(ORIGIN).split("#", 1)[0]
        if path == CEREMONY_PATH:
            await route.fulfill(content_type="text/html", body=_AUTHENTICATION_HTML)
        elif path == "/authentication-surface.js":
            await route.fulfill(content_type="text/javascript", body=_AUTHENTICATION_SCRIPT)
        elif path == CEREMONY_PATH + "/frame":
            await route.fulfill(content_type="image/png", body=ONE_PIXEL_PNG)
        elif path == CEREMONY_PATH + "/events" and request.method == "POST":
            assert request.headers["x-browser-ceremony-capability"] == "synthetic-capability"
            events.append(json.loads(request.post_data or "{}"))
            await route.fulfill(status=204)
        else:
            await route.fulfill(status=404)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.route(f"{ORIGIN}/**", serve)
        await page.goto(f"{ORIGIN}{CEREMONY_PATH}#capability=synthetic-capability")
        await page.wait_for_function("document.getElementById('frame').naturalWidth > 0")
        try:
            yield page, events
        finally:
            await browser.close()


async def _settled(page: Page, events: list[dict[str, object]], count: int) -> None:
    for _ in range(100):
        if len(events) >= count:
            return
        await page.wait_for_timeout(20)


async def test_keys_typed_after_clicking_the_frame_reach_the_website_one_at_a_time(
    surface: tuple[Page, list[dict[str, object]]],
) -> None:
    """Each key is relayed as it is pressed, in order, with no separate send step."""
    page, events = surface

    await page.click("#frame")
    await page.keyboard.type("ab")
    await page.keyboard.press("Backspace")
    await page.keyboard.press("Enter")
    await _settled(page, events, 5)

    assert [event["kind"] for event in events] == ["click", "text", "text", "key", "key"]
    assert events[1:] == [
        {"kind": "text", "text": "a"},
        {"kind": "text", "text": "b"},
        {"kind": "key", "key": "Backspace"},
        {"kind": "key", "key": "Enter"},
    ]


async def test_pasted_text_reaches_the_website_and_leaves_nothing_on_the_surface(
    surface: tuple[Page, list[dict[str, object]]],
) -> None:
    """A password-manager paste is relayed, and no typed value stays in this page."""
    page, events = surface

    await page.click("#frame")
    await page.keyboard.insert_text("synthetic-entry")
    await _settled(page, events, 2)

    assert events[1:] == [{"kind": "text", "text": "synthetic-entry"}]
    values = await page.evaluate(
        "[...document.querySelectorAll('input,textarea')].map(field => field.value)"
    )
    assert all(value == "" for value in values)


async def test_the_surface_has_no_separate_send_step(
    surface: tuple[Page, list[dict[str, object]]],
) -> None:
    """Typing goes straight to the website; nothing waits for a send button."""
    page, _ = surface

    assert await page.locator("#send").count() == 0
    assert await page.get_by_text("Send securely").count() == 0
