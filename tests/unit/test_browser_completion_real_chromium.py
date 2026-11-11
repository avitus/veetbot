"""A delayed server effect is reconciled through visible evidence, exactly once."""

from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.browser_control_plane.runtime import HostedPlaywrightSessionRuntime
from agent_core.domain.browser import (
    BROWSER_AUTH_INTERRUPTION_MARKER,
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserObservation,
)
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.tools.browser_act import BrowserActTool
from tests.contract.support import tool_context
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider


@pytest.mark.parametrize("kind", ["text", "region", "shadow_region", "row", "location", "hidden"])
async def test_delayed_receipt_does_not_duplicate_an_effect(kind: str) -> None:
    """Verify delayed receipt does not duplicate an effect."""
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        """Serve the synthetic page used to observe browser behavior."""
        del request
        return html(
            '<button id="save">Save</button><p hidden>Hidden confirmation</p><div id="host"></div>',
            script="const receiptRoot = "
            + (
                "document.querySelector('#host').attachShadow({mode:'open'});"
                if kind == "shadow_region"
                else "document.body;"
            )
            + """
            document.querySelector('#save').onclick = async () => {
                await fetch('/effect', {method:'POST'});
                setTimeout(() => {
                    const receipt = document.createElement('div');
                    receipt.innerHTML =
                        '<p role="status">Saved successfully</p>' +
                        '<table><tbody><tr><td>42</td></tr></tbody></table>';
                    receiptRoot.append(receipt);
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
        if kind in {"region", "shadow_region"}:
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


@pytest.mark.parametrize("shadow", [False, True])
async def test_sign_in_during_receipt_poll_preserves_one_server_effect(shadow: bool) -> None:
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="save">Save</button><div id="host"></div>',
            script="document.querySelector('#save').onclick = "
            "() => fetch('/effect', {method:'POST'});",
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with browsing([Route("/", home), Route("/effect", effect, methods=["POST"])]) as (
        site,
        runtime,
    ):
        hosted = HostedPlaywrightSessionRuntime(
            tenant_id="test",
            runtime=runtime,
            proxy_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        )

        class Provider(FakeBrowserProvider):
            def allows(self, url: str) -> bool:
                return url.startswith(site.origin + "/")

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                self.actions.append(action)
                assert constraint is None
                return await hosted.act(action)

            async def observe(self) -> BrowserObservation:
                self.observation_count += 1
                assert runtime._page is not None
                await runtime._page.evaluate(
                    """shadow => {
                        const host = document.querySelector('#host');
                        const root = shadow ? host.attachShadow({mode:'open'}) : host;
                        root.innerHTML = '<input type="password" value="SECRET_CANARY">';
                    }""",
                    shadow,
                )
                return await hosted.observe()

        before = await runtime.navigate(site.url("/"))
        provider = Provider()
        result = await BrowserActTool(provider).execute(
            {
                "kind": "click",
                "expected_revision": before.revision,
                "ref": before.elements[0].ref,
                "postcondition": {
                    "evidence": {"kind": "text", "text": "Saved successfully"},
                    "timeout_ms": 1500,
                },
            },
            tool_context(),
        )
        assert not result.ok and result.failure is not None
        assert result.failure.reason_code == "tool.browser.outcome_unknown"
        assert result.failure.external_text == BROWSER_AUTH_INTERRUPTION_MARKER
        assert not result.failure.retryable
        assert "SECRET_CANARY" not in result.model_dump_json()
        assert not runtime._elements and not runtime._regions
        assert effects == [True]
        assert len(provider.actions) == provider.observation_count == 1
