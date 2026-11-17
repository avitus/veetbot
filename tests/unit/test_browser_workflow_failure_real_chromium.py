"""Rendered failure evidence stops a recipe without repeating its server effect."""

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
    BrowserProviderError,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


@pytest.mark.parametrize("lost_response", [False, True])
@pytest.mark.parametrize(
    "markup,expected",
    [
        ("<p>Report rejected</p>", "observed_failure"),
        ("<p>Report ready</p><p>Report rejected</p>", "unverified"),
        ("<p>Pending</p><p hidden>Report rejected</p>", "unverified"),
    ],
)
async def test_rendered_failure_does_not_advance_or_repeat_the_action(
    monkeypatch: pytest.MonkeyPatch, lost_response: bool, markup: str, expected: str
) -> None:
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="open">Open activity</button><section id="status">Pending</section>',
            script="document.querySelector('#open').onclick = async () => {"
            "await fetch('/effect', {method:'POST'});"
            f"document.querySelector('#status').innerHTML = '{markup}';"
            "};",
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
        action = recipe["steps"][1]
        action["role"] = "button"
        action["postcondition"]["failure_evidence"] = {"kind": "text", "text": "Report rejected"}
        recipe["steps"].append(deepcopy(action))
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
                return await runtime.observe()

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                self.actions.append(action)
                result = await runtime.act(action, constraint=constraint)
                if lost_response:
                    raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
                return result

        provider = Provider(allowed_origins=(site.origin,))
        async with bootstrap.build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(
                turns=[
                    ScriptedTurn(
                        text="Reported the observed outcome.",
                        context_contains="Report rejected"
                        if expected == "observed_failure"
                        else None,
                    )
                ]
            ),
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
            assert len(approvals) == 1
            assert not effects and not provider.actions
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            assert not await app.approvals.list_pending(run_id=run_id)
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run_id)
            assert saved is not None
            assert saved.working_state["browser_workflow"]["status"] == expected
            assert saved.working_state["browser_workflow"]["completed_steps"] == 1
            assert saved.working_state["browser_workflow_report_only"]
        assert effects == [True]
        assert len(provider.actions) == 1
        if expected == "observed_failure" and not lost_response:
            assert provider.observation_count == 1
