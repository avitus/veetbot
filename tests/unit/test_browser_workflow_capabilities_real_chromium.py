"""Unsupported recipes make no real requests; compatible ones retain approval."""

from copy import deepcopy

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
    BrowserProfile,
    BrowserProfileStatus,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


@pytest.mark.parametrize("missing", ["browser.observe", "browser.act", None])
async def test_pinned_capability_check_precedes_real_navigation(
    monkeypatch: pytest.MonkeyPatch, missing: str | None
) -> None:
    requests: list[str] = []
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        requests.append(request.url.path)
        return html(
            '<button id="open">Open activity</button><p id="status">Pending</p>',
            script="""document.querySelector('#open').onclick = async () => {
                await fetch('/effect', {method:'POST'});
                document.querySelector('#status').textContent = 'Report ready';
            };""",
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with browsing([Route("/account", home), Route("/effect", effect, methods=["POST"])]) as (
        site,
        runtime,
    ):
        catalog = deepcopy(CATALOG)
        recipe = catalog["recipes"][0]
        recipe["origin"] = site.origin
        recipe["steps"][0]["url"] = site.url("/account")
        recipe["steps"][1]["role"] = "button"
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

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                self.actions.append(action)
                return await runtime.act(action, constraint=constraint)

        provider = Provider(allowed_origins=(site.origin,))
        async with bootstrap.build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(turns=[ScriptedTurn(text="Reported available progress.")]),
            enabled_tools=[
                name
                for name in ("browser.navigate", "browser.observe", "browser.act")
                if name != missing
            ],
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
            assert len(approvals) == int(missing is None)
            assert not effects and not provider.actions
            if approvals:
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            result = await app.runs.wait_terminal(run_id)
            assert result.status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run_id)
            assert saved is not None
            if missing is None:
                assert saved.working_state["browser_workflow"]["status"] == "verified"
            else:
                assert "browser_workflow" not in saved.working_state
                assert not saved.working_state.get("browser_workflow_report_only")
                assert result.tool_call_count == 0
        assert requests == (["/account"] if missing is None else [])
        assert effects == [True] * int(missing is None)
        assert len(provider.actions) == int(missing is None)
