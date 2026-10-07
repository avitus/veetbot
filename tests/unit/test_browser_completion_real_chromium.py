"""A delayed server effect is reconciled through visible evidence, exactly once."""

from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser import BrowserAction, BrowserDispatchConstraint, BrowserObservation
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.tools.browser_act import BrowserActTool
from tests.contract.support import tool_context
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider


@pytest.mark.parametrize("kind", ["text", "region", "row", "location", "hidden"])
async def test_delayed_receipt_does_not_duplicate_an_effect(kind: str) -> None:
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="save">Save</button><p hidden>Hidden confirmation</p>',
            script="""
            document.querySelector('#save').onclick = async () => {
                await fetch('/effect', {method:'POST'});
                setTimeout(() => {
                    document.body.insertAdjacentHTML('beforeend',
                        '<p role="status">Saved successfully</p>' +
                        '<table><tbody><tr><td>42</td></tr></tbody></table>');
                    history.replaceState({}, '', '/complete');
                }, 850);
            };
        """,
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with browsing([Route("/", home), Route("/effect", effect, methods=["POST"])]) as (
        site,
        runtime,
    ):

        class Provider(FakeBrowserProvider):
            def allows(self, url: str) -> bool:
                return url.startswith(site.origin + "/")

            async def observe(self) -> BrowserObservation:
                return await runtime.observe()

            async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
                return await runtime.extract(request)

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                return await runtime.act(action, constraint=constraint)

        evidence: dict[str, Any] = {"kind": "text", "text": "Saved successfully"}
        if kind == "region":
            evidence.update(kind="region", region_kind="status")
        elif kind == "row":
            evidence = {
                "kind": "row",
                "collection_kind": "table",
                "fields": [{"name": "receipt", "column": 0, "type": "integer", "value": 42}],
            }
        elif kind == "location":
            evidence = {"kind": "location", "url": site.url("/complete")}
        elif kind == "hidden":
            evidence["text"] = "Hidden confirmation"
        before = await runtime.navigate(site.url("/"))
        result = await BrowserActTool(Provider()).execute(
            {
                "kind": "click",
                "expected_revision": before.revision,
                "ref": before.elements[0].ref,
                "postcondition": {"evidence": evidence, "timeout_ms": 1500},
            },
            tool_context(),
        )
        assert result.ok and result.structured is not None, (result.model_dump(), effects)
        assert result.structured["condition"]["status"] == (
            "not_observed" if kind == "hidden" else "satisfied"
        )
        assert effects == [True]
