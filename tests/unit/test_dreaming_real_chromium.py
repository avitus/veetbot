"""Real browser controls exercise the authenticated API and actual memory writer."""

from dataclasses import replace
from typing import cast

from playwright.async_api import Route, async_playwright, expect

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.memory.reconsolidation_apply import apply_review
from tests.contract.reconsolidation_apply_cases import prepared_batch
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_surface_cases import owner
from tests.contract.support import NOW, session
from tests.gates.test_memory_api_boundary_m17 import _client
from tests.integration.m2_support import memory_settings
from tests.real_browser_support import require_real_browser


async def test_real_review_approve_undo_reject_pause_and_mobile() -> None:
    require_real_browser()
    settings = replace(
        memory_settings(),
        memory_api_enabled=True,
        memory_reconsolidation_api_enabled=True,
        memory_dreaming_review_enabled=True,
    )
    async with build(
        settings=settings, storage="memory", principal=owner(), clock=FixedClock(NOW)
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        prepared, review = await prepared_batch(cast(Factory, app.uow_factory))
        await apply_review(
            app.uow_factory,
            FixedClock(NOW),
            owner(),
            prepared,
            review,
            admitted=lambda: True,
            owner_review=True,
        )
        async with _client(app) as client, async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route(request: Route) -> None:
                incoming = request.request
                response = await client.request(
                    incoming.method,
                    incoming.url,
                    content=incoming.post_data,
                    headers=incoming.headers,
                )
                await request.fulfill(
                    status=response.status_code,
                    headers=dict(response.headers),
                    body=response.content,
                )

            await page.route("**/*", route)
            await page.goto("http://127.0.0.1/dreaming")
            await page.get_by_role("button", name="Connect", exact=True).click()
            await expect(page.locator(".card")).to_have_count(2)
            await page.get_by_role("button", name="Approve and apply", exact=True).click()
            await page.locator("#confirm-button").click()
            await expect(page.locator(".card")).to_have_count(1)
            await page.get_by_role("button", name="Applied", exact=True).click()
            await expect(page.locator(".card")).to_have_count(1)
            await page.get_by_role("button", name="Undo merge", exact=True).click()
            await page.locator("#confirm-button").click()
            await expect(page.locator(".card")).to_have_count(0)
            await page.get_by_role("button", name="Waiting for you", exact=True).click()
            await page.get_by_role("button", name="Reject proposal", exact=True).click()
            await page.locator("#confirm-button").click()
            await expect(page.locator(".card")).to_have_count(0)
            await page.get_by_role("button", name="Review settings", exact=True).click()
            await page.get_by_role("button", name="Pause previews", exact=True).click()
            await expect(
                page.get_by_role("button", name="Resume previews", exact=True)
            ).to_be_visible()
            await page.reload()
            await expect(page.locator("#connect")).to_be_visible()
            await page.get_by_role("button", name="Connect", exact=True).click()
            await page.get_by_role("button", name="All history", exact=True).click()
            await expect(page.locator(".card")).to_have_count(2)
            await page.screenshot(path="/tmp/dreaming-desktop.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path="/tmp/dreaming-mobile.png", full_page=True)
            assert await page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
            assert not errors
            await browser.close()
