"""Paged workflow targets keep fresh references, approvals and single effects."""

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
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


@pytest.mark.parametrize(
    "controls,shadow,loss,replace",
    [
        # 768 controls is the plain found-and-verified path; 300 controls is
        # found after one expansion and varies shadow, lost response and replace.
        (300, True, False, False),
        (768, False, False, False),
        (1100, False, False, False),
        (300, False, True, False),
        (300, False, False, True),
    ],
)
async def test_workflow_discovers_a_current_target_without_replaying_an_effect(
    monkeypatch: pytest.MonkeyPatch,
    controls: int,
    shadow: bool,
    loss: bool,
    replace: bool,
) -> None:
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            "".join(f"<button>Choice {i}</button>" for i in range(controls))
            + '<div id="host"></div>',
            script="const host = document.querySelector('#host'); const root = "
            + ("host.attachShadow({mode:'open'});" if shadow else "host;")
            + """
            root.innerHTML = '<button id="target">Open activity</button><p id="status">Pending</p>';
            root.querySelector('#target').onclick = async () => {
                await fetch('/effect', {method:'POST'});
                root.querySelector('#status').textContent = 'Report ready';
            };
            """,
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
        catalog["recipes"][0]["origin"] = site.origin
        catalog["recipes"][0]["steps"][0]["url"] = site.url("/account")
        catalog["recipes"][0]["steps"][1]["role"] = "button"
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
            def __init__(self) -> None:
                super().__init__(allowed_origins=(site.origin,))
                self.expanded: list[BrowserObservation] = []

            async def navigate(self, url: str) -> BrowserObservation:
                return await runtime.navigate(url)

            async def observe(self) -> BrowserObservation:
                self.observation_count += 1
                return await runtime.observe()

            async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
                observed = await runtime.expand(request)
                self.expanded.append(observed)
                return observed

            async def act(
                self,
                action: BrowserAction,
                *,
                constraint: BrowserDispatchConstraint | None = None,
            ) -> BrowserObservation:
                self.actions.append(action)
                result = await runtime.act(action, constraint=constraint)
                if loss:
                    raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
                return result

        provider = Provider()
        async with bootstrap.build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(turns=[ScriptedTurn(text="Checked the available evidence.")]),
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
            found = controls < 1024
            assert len(approvals) == int(found)
            assert not effects and not provider.actions
            if found:
                if replace:
                    await runtime._current_page().evaluate("""() => {
                        const old = document.querySelector('#target');
                        const next = old.cloneNode(true);
                        next.onclick = old.onclick;
                        old.replaceWith(next);
                    }""")
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                checkpoint = await uow.checkpoints.latest(run_id)
            assert checkpoint is not None
            state = checkpoint.working_state["browser_workflow"]
            assert state["status"] == (
                "target_not_observed" if not found else "unverified" if replace else "verified"
            )
            assert state["target_search"]["windows"] == len(provider.expanded) + 1
            assert state["target_search"]["windows"] <= 4
            assert checkpoint.working_state["browser_workflow_report_only"]
            events = await app.runs.events(run_id)
            assert not any(event.event_type == "run.waiting_for_user" for event in events)
        assert effects == [True] * int(found and not replace)
        assert len(provider.actions) == int(found)
        if found:
            assert provider.actions[0].expected_revision == provider.expanded[-1].revision
            assert provider.actions[0].ref in {
                element.ref for element in provider.expanded[-1].elements
            }
