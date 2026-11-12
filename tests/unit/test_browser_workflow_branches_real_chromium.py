"""Reviewed branch choices use rendered evidence and the ordinary approval pipeline."""

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
from tests.unit.test_browser_workflow_branches import branch_catalog
from tests.unit.test_config import base_environment


@pytest.mark.parametrize(
    "initial,receipt,lost_response,expected",
    [
        ("Report ready", True, False, "verified"),
        ("Summary ready", True, False, "verified"),
        ("Report closed", True, False, "verified"),
        ("Report closed", True, True, "verified"),
        ("Report closed", False, False, "unverified"),
        ("Pending", True, False, "unverified"),
        ("Report ready<br>Report closed", True, False, "unverified"),
    ],
)
async def test_rendered_branch_skips_or_approves_exactly_one_effect(
    monkeypatch: pytest.MonkeyPatch,
    initial: str,
    receipt: bool,
    lost_response: bool,
    expected: str,
) -> None:
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            f'<p id="state">{initial}</p><button id="open">Open activity</button>',
            script="document.querySelector('#open').onclick = async () => {"
            "await fetch('/effect', {method:'POST'});"
            + ("document.querySelector('#state').textContent = 'Report ready';" if receipt else "")
            + "};",
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with browsing([Route("/account", home), Route("/effect", effect, methods=["POST"])]) as (
        site,
        runtime,
    ):
        catalog = branch_catalog()
        recipe = catalog["recipes"][0]
        recipe["origin"] = site.origin
        recipe["steps"][0]["url"] = site.url("/account")
        recipe["steps"][2]["role"] = "button"
        if initial == "Summary ready":
            from copy import deepcopy

            recipe["steps"].append(deepcopy(recipe["steps"][2]))
            recipe["steps"][2]["name"] = "Never choose"
            recipe["steps"][1]["postcondition"]["evidence"]["text"] = "Summary ready"
            recipe["steps"][1]["branches"] = {"satisfied": 3, "failed": 4}
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
                self.navigations.append(url)
                return await runtime.navigate(url)

            async def observe(self) -> BrowserObservation:
                self.observation_count += 1
                return await runtime.observe()

            async def act(
                self,
                action: BrowserAction,
                *,
                constraint: BrowserDispatchConstraint | None = None,
            ) -> BrowserObservation:
                self.actions.append(action)
                observed = await runtime.act(action, constraint=constraint)
                if lost_response:
                    raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
                return observed

        provider = Provider(allowed_origins=(site.origin,))
        async with bootstrap.build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(turns=[ScriptedTurn(text="Report evidence checked.")]),
            enabled_tools=[
                "browser.navigate",
                "browser.observe",
                "browser.act",
                "conversation.ask_user",
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
                app.principal,
                "general",
                {},
                browser_profile_id=PROFILE_ID,
            )
            run_id = await app.runs.submit("Read my saved report.", session.id)
            approvals = await app.approvals.list_pending(run_id=run_id)
            needs_action = initial in {"Report closed", "Summary ready"}
            assert len(approvals) == int(needs_action)
            assert not effects and not provider.actions
            if needs_action:
                async with app.uow_factory() as uow:
                    paused = await uow.checkpoints.latest(run_id)
                assert paused is not None
                state = paused.working_state["browser_workflow"]
                destination = 3 if initial == "Summary ready" else 2
                assert state["last_branch"] == {
                    "step": 1,
                    "status": "satisfied" if initial == "Summary ready" else "failed",
                    "next_step": destination,
                }
                assert state["completed_steps"] == 2 and state["next_step"] == destination
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            result = await app.runs.wait_terminal(run_id)
            assert result.status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                checkpoint = await uow.checkpoints.latest(run_id)
            assert checkpoint is not None
            state = checkpoint.working_state["browser_workflow"]
            assert state["status"] == expected
            if initial == "Summary ready":
                assert state["completed_steps"] == 3 and state["next_step"] == 4
            assert checkpoint.working_state["browser_workflow_report_only"]
            events = await app.runs.events(run_id)
            assert not any(event.event_type == "run.waiting_for_user" for event in events)
        assert effects == [True] * int(needs_action)
        assert len(provider.actions) == int(needs_action)
