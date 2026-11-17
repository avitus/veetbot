"""A reviewed region selects one real control without inheriting its approval."""

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
from tests.unit.test_browser_workflow_regions import region_catalog
from tests.unit.test_config import base_environment


@pytest.mark.parametrize("mode", ["normal", "shadow", "lost_response", "replaced", "duplicate"])
async def test_real_region_selects_only_the_intended_approved_effect(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    effects: list[str] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="outside">Open activity</button><div id="host"></div>'
            '<p id="status">Pending</p>',
            script="const host = document.querySelector('#host'); const root = "
            + ("host.attachShadow({mode:'open'});" if mode == "shadow" else "host;")
            + """
            root.innerHTML = '<form><p>Account activity</p>' +
                '<button type="button">Open activity</button></form>';
            document.querySelector('#outside').onclick = () =>
                fetch('/effect/outside', {method:'POST'});
            root.querySelector('button').onclick = async () => {
                await fetch('/effect/inside', {method:'POST'});
                document.querySelector('#status').textContent = 'Report ready';
            };
            """
            + (
                "root.append(root.querySelector('form').cloneNode(true));"
                if mode == "duplicate"
                else ""
            ),
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
        catalog = region_catalog()
        recipe = catalog["recipes"][0]
        recipe["origin"] = site.origin
        recipe["steps"][0]["url"] = site.url("/account")
        recipe["steps"][1]["role"] = "button"
        recipe["steps"][1]["target_region"]["text"] = "Account activity Open activity"
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
                return await runtime.observe()

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
            assert len(approvals) == int(mode != "duplicate")
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
                "ambiguous_region"
                if mode == "duplicate"
                else "unverified"
                if mode == "replaced"
                else "verified"
            )
            assert saved.working_state["browser_workflow"]["status"] == expected
            assert saved.working_state["browser_workflow_report_only"]
        assert effects == ([] if mode in {"duplicate", "replaced"} else ["inside"])
        assert len(provider.actions) == int(mode != "duplicate")
