"""A bounded readiness wait precedes fresh region targeting and ordinary approval."""

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserObservation,
    BrowserObservationExpansion,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflow_readiness import readiness_catalog
from tests.unit.test_config import base_environment


@pytest.mark.parametrize(
    "mode", ["delayed", "missing", "failure", "ambiguous", "lost_response", "replaced"]
)
async def test_real_readiness_precedes_fresh_region_targeting(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    effects: list[str] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="outside">Open activity</button><div id="host"></div>'
            '<p id="status">Pending</p><p id="ready">Pending</p>',
            script="const host = document.querySelector('#host'); const root = "
            + "host;"
            + """
            root.innerHTML = '<form><p>Account activity</p>' +
                '<button type="button">Open activity</button></form>';
            document.querySelector('#outside').onclick = () =>
                fetch('/effect/outside', {method:'POST'});
            root.querySelector('button').onclick = async () => {
                await fetch('/effect/inside', {method:'POST'});
                document.querySelector('#status').textContent = 'Report ready';
            };
            """,
        )

    async def effect(request: Request) -> Response:
        effects.append(request.path_params["target"])
        return Response("ok")

    async with browsing(
        [Route("/account", home), Route("/effect/{target}", effect, methods=["POST"])]
    ) as (
        site,
        runtime,
    ):
        catalog = readiness_catalog()
        recipe = catalog["recipes"][0]
        recipe["origin"] = site.origin
        recipe["steps"][0]["url"] = site.url("/account")
        recipe["steps"][1]["role"] = "button"
        recipe["steps"][1]["precondition"]["timeout_ms"] = 1500
        recipe["steps"][1]["target_region"] = {
            "kind": "region",
            "region_kind": "form",
            "text": "Account activity Open activity",
        }
        monkeypatch.setattr(
            bootstrap,
            "load_config_document",
            lambda settings, path: (
                catalog
                if path == "runtime/browser-workflows.yaml"
                else load_config_document(settings, path)
            ),
        )

        class Provider(FakeBrowserProvider):
            async def navigate(self, url: str) -> BrowserObservation:
                return await runtime.navigate(url)

            async def observe(self) -> BrowserObservation:
                self.observation_count += 1
                result = await runtime.observe()
                if self.observation_count == 1 and mode != "missing":
                    assert runtime._page is not None
                    text = (
                        "Account unavailable"
                        if mode == "failure"
                        else "Account ready\nAccount unavailable"
                        if mode == "ambiguous"
                        else "Account ready"
                    )
                    # Return genuinely pending evidence, then let the page update
                    # while the production wait polls. No synthetic condition result.
                    await runtime._page.evaluate(
                        "text => setTimeout(() => {"
                        " document.querySelector('#ready').innerText = text; }, 100)",
                        text,
                    )
                return result

            async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
                return await runtime.expand(request)

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                self.actions.append(action)
                result = await runtime.act(action, constraint=constraint)
                if mode == "lost_response":
                    raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
                return result

        provider = Provider(allowed_origins=(site.origin,))
        async with bootstrap.build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(turns=[ScriptedTurn(text="Reported observed progress.")]),
            enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
            browser_provider_override=provider,
        ) as app:
            async with app.uow_factory() as uow:
                await uow.browser_profiles.create(
                    BrowserProfile(
                        id=PROFILE_ID,
                        tenant_id=app.principal.tenant_id,
                        principal_id=app.principal.principal_id,
                        provider_name="hosted-isolated",
                        provider_ref="fixture",
                        allowed_origins=(site.origin,),
                        status=BrowserProfileStatus.READY,
                        generation=1,
                        encryption_key_version="key-v1",
                        created_at=app.clock.now(),
                        updated_at=app.clock.now(),
                    )
                )
            session = await app.services.sessions.create(
                app.principal, "general", {}, browser_profile_id=PROFILE_ID
            )
            run_id = await app.runs.submit("Read my saved report.", session.id)
            approvals = await app.approvals.list_pending(run_id=run_id)
            ready = mode in {"delayed", "lost_response", "replaced"}
            assert len(approvals) == int(ready)
            assert provider.observation_count >= 2
            assert not effects and not provider.actions
            if mode == "replaced":
                assert runtime._page is not None
                await runtime._page.locator("#host button").evaluate(
                    "node => node.replaceWith(node.cloneNode(true))"
                )
            if approvals:
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run_id)
            assert saved is not None
            expected = (
                "observed_failure"
                if mode == "failure"
                else "verified"
                if mode in {"delayed", "lost_response"}
                else "unverified"
            )
            assert saved.working_state["browser_workflow"]["status"] == expected
            assert saved.working_state["browser_workflow_report_only"]
        assert effects == (["inside"] if mode in {"delayed", "lost_response"} else [])
        assert len(provider.actions) == int(ready)
