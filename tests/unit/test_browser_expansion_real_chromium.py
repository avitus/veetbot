"""Bounded live observation windows through real Chromium (ADR-0157)."""

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser import BrowserObservationExpansion, BrowserProviderError
from agent_core.ports.browser import expand_browser_observation
from tests.unit.test_browser_runtime_real_chromium import browsing, html


async def test_expansion_reaches_controls_beyond_the_first_window_with_new_references() -> None:
    async def home(request: Request) -> Response:
        del request
        return html("".join(f"<button>Choice {i}</button>" for i in range(300)))

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        assert len(first.elements) == 256
        expanded = await expand_browser_observation(
            runtime, BrowserObservationExpansion(after=first.elements[9].ref)
        )
        assert expanded.elements[0].name == "Choice 10"
        assert expanded.revision != first.revision
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await expand_browser_observation(
                runtime, BrowserObservationExpansion(after=first.elements[9].ref)
            )
        assert expanded.coverage is not None and expanded.coverage.next_cursor is not None
        final = await expand_browser_observation(
            runtime, BrowserObservationExpansion(cursor=expanded.coverage.next_cursor)
        )
        assert final.elements[-1].name == "Choice 299"
        assert final.coverage is not None and final.coverage.next_cursor is None


async def test_empty_hidden_window_has_a_usable_cursor_and_bounded_handles() -> None:
    async def home(request: Request) -> Response:
        del request
        return html("<button hidden>Hidden</button>" * 5000 + "<button>Reachable</button>")

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        assert not first.elements
        assert first.coverage is not None and first.coverage.next_cursor is not None
        assert first.coverage.scanned_candidates == 4096
        page = runtime._current_page()
        acquired = await page.locator("veetbot_controls=0").element_handles()
        assert len(acquired) == 4097
        for handle in acquired:
            await handle.dispose()
        expanded = await expand_browser_observation(
            runtime, BrowserObservationExpansion(cursor=first.coverage.next_cursor)
        )
        assert [element.name for element in expanded.elements] == ["Reachable"]
        assert expanded.coverage is not None and expanded.coverage.next_cursor is None


async def test_anchor_movement_and_document_reload_refuse_before_expansion() -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<button id="anchor">Anchor</button><button>Next</button>')

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        await runtime._current_page().evaluate("document.querySelector('#anchor').remove()")
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await expand_browser_observation(
                runtime, BrowserObservationExpansion(after=first.elements[0].ref)
            )
        first = await runtime.navigate(site.url("/"))
        await runtime._current_page().reload()
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await expand_browser_observation(
                runtime, BrowserObservationExpansion(after=first.elements[0].ref)
            )


async def test_native_and_aria_labels_identify_form_controls_without_values() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<label>Answer<input value="private stored value"></label>'
            '<span id="description">Preferred choice</span>'
            '<input aria-labelledby="description" aria-label="Fallback">'
            '<label for="choice">Choice</label><select id="choice"><option>Yes</option></select>'
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        page = await runtime.navigate(site.url("/"))
        assert [element.name for element in page.elements] == [
            "Answer",
            "Preferred choice",
            "Choice",
        ]
        assert "private stored value" not in page.model_dump_json()


async def test_scan_limit_is_explicit_and_a_foreign_cursor_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_core.adapters.browser.playwright.MAXIMUM_CANDIDATE_OFFSET", 8)

    async def home(request: Request) -> Response:
        del request
        return html("<button hidden>Hidden</button>" * 10 + "<button>Later</button>")

    async with browsing([Route("/", home)]) as (site, runtime):
        page = await runtime.navigate(site.url("/"))
        assert page.coverage is not None and page.coverage.scan_limit_reached
        assert not page.elements and page.coverage.next_cursor is None
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await expand_browser_observation(runtime, BrowserObservationExpansion(cursor="f" * 32))
