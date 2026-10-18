"""Correct invalid requests using the same live region, with no browser replay."""

import json
from dataclasses import replace
from uuid import UUID

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserObservation,
    BrowserObservationExpansion,
)
from agent_core.domain.browser_projection import browser_context_projection
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.tools.browser_act import BrowserActTool
from agent_core.tools.browser_observe import ArticleBrowserObserveTool
from tests.contract.support import tool_context
from tests.unit.test_browser_request_guidance import ReferenceBrowser
from tests.unit.test_browser_runtime_real_chromium import browsing, html


async def test_live_region_correction_preserves_focus_cursor_and_scrolls_once() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<main><button>Read more</button><p>"
            + "Visible reading evidence. " * 500
            + '</p><div style="height:2500px"></div></main>'
        )

    async with browsing([Route("/", home)]) as (site, runtime):

        class Provider(ReferenceBrowser):
            def allows(self, url: str) -> bool:
                return url.startswith(site.origin + "/")

            async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
                self.expansions.append(request)
                self.current = await runtime.expand(request)
                return self.current

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                self.actions.append(action)
                self.current = await runtime.act(action, constraint=constraint)
                return self.current

        provider = Provider()
        initial = provider.current = await runtime.navigate(site.url("/"))
        region = next(region for region in initial.regions if region.kind == "main")
        marks = []

        async def mark() -> None:
            marks.append(True)

        context = replace(tool_context(), mark_effect_sent=mark)
        act, observe = BrowserActTool(provider), ArticleBrowserObserveTool(provider)
        refused_action = await act.execute(
            {
                "kind": "scroll",
                "expected_revision": initial.revision,
                "ref": region.ref,
                "delta_y": 1000,
            },
            context,
        )
        refused_read = await observe.execute(
            {"region_ref": region.ref, "expected_revision": initial.revision, "text_offset": 1000},
            context,
        )
        assert refused_action.failure is not None
        assert refused_action.failure.reason_code == "tool.browser.region_not_actionable"
        assert refused_read.failure is not None
        assert refused_read.failure.reason_code == "tool.browser.focus_required"
        assert not provider.actions and not provider.expansions and not marks
        assert runtime._page is not None
        assert await runtime._page.evaluate("window.scrollY") == 0
        focused = await observe.execute(
            {"region_ref": region.ref, "expected_revision": initial.revision}, context
        )
        assert focused.ok and focused.structured is not None
        projected = browser_context_projection(
            focused.structured,
            budget=1024,
            reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
        )
        assert projected is not None
        view = json.loads(next(part.text for part in projected if isinstance(part, TextPart)))
        continued = await observe.execute(view["next_text"], context)
        assert continued.ok
        current = provider.current
        scrolled = await act.execute(
            {
                "kind": "scroll",
                "expected_revision": current.revision,
                "ref": current.elements[0].ref,
                "delta_y": 1000,
            },
            context,
        )
        assert scrolled.ok
        assert len(provider.expansions) == 2 and len(provider.actions) == len(marks) == 1
        assert await runtime._page.evaluate("window.scrollY") > 0
