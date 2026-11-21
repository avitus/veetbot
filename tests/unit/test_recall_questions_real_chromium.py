"""The recall question page, driven in a real browser against the composed API."""

from dataclasses import replace
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from playwright.async_api import Route, async_playwright, expect
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.api.app import create_app
from agent_core.bootstrap import build
from agent_core.config import AuthMode
from agent_core.domain.agents import Principal
from agent_core.domain.recall_questions import QuestionPathResult, QuestionResult
from tests.contract.owner_recall_probe_cases import stated
from tests.contract.support import NOW
from tests.integration.m2_support import memory_settings
from tests.real_browser_support import require_real_browser

# What the owner pastes into the page; synthetic, and named so no scanner reads it as real.
_PASTED = "synthetic-question-page-value"
_HOSTILE = "Where do I live? <img src=x onerror=alert(1)>"
_SHOTS = Path(__file__).resolve().parents[2] / "test-results" / "recall-questions"


async def test_the_owner_keeps_the_question_set_from_the_page() -> None:
    require_real_browser()
    settings = replace(memory_settings(), memory_api_enabled=True, recall_questions_enabled=True)
    owner = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes={"memory.read", "memory.write"},
    )
    async with build(
        settings=settings, storage="memory", principal=owner, clock=FixedClock(NOW)
    ) as app:
        session_id = await app.sessions.create()
        await stated(app, session_id, "User lives in Example Town", "home")
        api_app = create_app(
            app.services,
            replace(settings, auth_mode=AuthMode.TOKEN, auth_token=SecretStr(_PASTED)),
            owner,
            app.new_request_id,
            app.readiness_probe,
            clock=app.clock,
        )
        async with (
            AsyncClient(
                transport=ASGITransport(app=api_app), base_url="https://agent.test"
            ) as client,
            async_playwright() as playwright,
        ):
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
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
            await page.goto("https://agent.test/recall-questions")

            # Signed out, the page shows nothing but the way in; a wrong token says so.
            await expect(page.locator("#connect")).to_be_visible()
            await expect(page.locator("#workspace")).to_be_hidden()
            await page.locator("#token").fill("not-the-token")
            await page.get_by_role("button", name="Connect", exact=True).click()
            await expect(page.locator("#error")).to_contain_text("was not accepted")
            await page.locator("#token").fill(_PASTED)
            await page.get_by_role("button", name="Connect", exact=True).click()
            await expect(page.locator("#workspace")).to_be_visible()
            await expect(page.locator("#summary")).to_contain_text("0 active questions")

            # An incomplete question is refused in place, before any request.
            await page.get_by_role("button", name="Add question", exact=True).click()
            await page.locator("#field-question").fill(_HOSTILE)
            await page.get_by_role("button", name="Save question", exact=True).click()
            await expect(page.locator("#editor-error")).to_contain_text("at least one fact")

            # Pick the fact from the store instead of typing it.
            await page.locator("#memory-search").fill("Example Town")
            await page.get_by_role("button", name="Search memories", exact=True).click()
            await expect(page.locator(".memory")).to_have_count(1)
            await page.get_by_role("button", name="Reach this text", exact=True).click()
            await page.get_by_role("button", name="Save question", exact=True).click()
            await expect(page.locator("#editor-error")).to_contain_text("Give the question a key")
            await page.locator("#field-key").fill("home-city")
            await page.get_by_role("button", name="Save question", exact=True).click()
            await expect(page.locator("#editor-error")).to_contain_text("Choose the half")
            await page.locator('input[name="split"][value="holdout"]').check()
            await page.get_by_role("button", name="Save question", exact=True).click()

            await expect(page.locator("#editor")).not_to_be_visible()
            await expect(page.locator(".card")).to_have_count(1)
            card = page.locator(".card")
            await expect(card).to_contain_text(_HOSTILE)
            await expect(card).to_contain_text("User lives in Example Town")
            await expect(card.locator(".pill.holdout")).to_have_text("Held out")
            await expect(card).to_contain_text("Not run yet")
            # Owner text is shown as text: nothing it contains becomes markup.
            assert await page.locator("#list img").count() == 0
            await expect(page.locator("#summary")).to_contain_text("1 active question")
            await expect(page.locator("#summary")).to_contain_text("1 held out")
            _SHOTS.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(_SHOTS / "desktop.png"), full_page=True)

            # Editing keeps the key and the half, and says so.
            await card.get_by_role("button", name="Edit", exact=True).click()
            await expect(page.locator("#once-summary")).to_contain_text("Held out half")
            await expect(page.locator("#once-fields")).to_be_hidden()
            await page.locator("#field-question").fill("Which town do I live in?")
            await page.screenshot(path=str(_SHOTS / "editor.png"))
            await page.get_by_role("button", name="Save question", exact=True).click()
            await expect(page.locator(".card")).to_contain_text("Which town do I live in?")

            await page.locator(".card").get_by_role("button", name="History", exact=True).click()
            await expect(page.locator("#history-list .revision")).to_have_count(2)
            await expect(page.locator("#history-list")).to_contain_text("Version 2")
            await page.get_by_role("button", name="Close", exact=True).click()

            # A recorded run shows on the question without changing it.
            assert app.services.recall_questions is not None
            (stored,) = (await app.services.recall_questions.list_questions(owner)).items
            await app.services.recall_questions.record_results(
                owner,
                {
                    "home-city": QuestionResult(
                        recorded_at=NOW,
                        report_digest="c" * 64,
                        # The run tested the question as the page last saved it.
                        content_sha256=stored.content.criteria_sha256,
                        paths=(
                            QuestionPathResult(
                                path="automatic",
                                stages=("ranked",),
                                required_total=1,
                                required_delivered=0,
                            ),
                        ),
                    )
                },
            )
            await page.get_by_role("button", name="Reload", exact=True).click()
            await expect(page.locator(".card .pill.missed")).to_have_text("Missed")
            await expect(page.locator(".card .paths")).to_contain_text(
                "0 of 1 required facts delivered (ranked)"
            )
            await page.locator("#filter-result").select_option("passed")
            await expect(page.locator("#empty")).to_be_visible()
            await page.locator("#filter-result").select_option("")

            # Retiring takes a question out of runs without losing it.
            await page.locator(".card").get_by_role("button", name="Retire", exact=True).click()
            await expect(page.locator(".card")).to_have_count(0)
            await expect(page.locator("#summary")).to_contain_text("0 active questions")
            await page.locator("#filter-status").select_option("retired")
            await expect(page.locator(".card")).to_have_count(1)
            await page.locator(".card").get_by_role("button", name="Restore", exact=True).click()
            await page.locator("#filter-status").select_option("active")
            await expect(page.locator(".card")).to_have_count(1)

            # A change refused because the question moved on stays explained
            # after the list reloads; the reload alone must not wipe the reason.
            (current,) = (await app.services.recall_questions.list_questions(owner)).items
            await app.services.recall_questions.edit(
                owner,
                current.id,
                expected_revision=current.revision,
                content=current.content.model_copy(update={"notes": "edited elsewhere"}),
            )
            await page.locator(".card").get_by_role("button", name="Retire", exact=True).click()
            await expect(page.locator("#error")).to_contain_text("the question changed")
            await expect(page.locator(".card")).to_have_count(1)
            await expect(page.locator("#summary")).to_contain_text("1 active question")
            await expect(page.locator("#error")).to_be_visible()
            # The reloaded card holds the new revision, so the same click now works.
            await page.locator(".card").get_by_role("button", name="Retire", exact=True).click()
            await expect(page.locator(".card")).to_have_count(0)
            await expect(page.locator("#error")).to_be_hidden()
            await page.locator("#filter-status").select_option("retired")
            await page.locator(".card").get_by_role("button", name="Restore", exact=True).click()
            await page.locator("#filter-status").select_option("active")
            await expect(page.locator(".card")).to_have_count(1)

            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(_SHOTS / "mobile.png"), full_page=True)

            # The token is never stored: a reload asks for it again.
            assert await page.evaluate(
                "localStorage.length === 0 && sessionStorage.length === 0 && document.cookie === ''"
            )
            await page.reload()
            await expect(page.locator("#connect")).to_be_visible()
            await expect(page.locator("#workspace")).to_be_hidden()
            assert not errors
            await browser.close()
