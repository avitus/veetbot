"""Later live windows prove their own destination without carrying stale proof."""

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
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, ToolCallItem
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflow_destination_recheck import recheck_catalog
from tests.unit.test_config import base_environment


@pytest.mark.parametrize(
    "mode",
    [
        "continuation",
        "region",
        "shadow_region",
        "changed",
        "region_changed",
        "history",
        "failure",
        "region_failure",
        "conflict",
        "lost_response",
        "replaced",
    ],
)
async def test_live_destination_recheck_preserves_approval_and_single_effect(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    effects: list[bool] = []
    focused = "region" in mode
    form = (
        '<form><p id="ready">Account ready</p><p>Account activity</p>'
        '<button type="button" id="target">Open activity</button></form>'
    )

    async def home(request: Request) -> Response:
        del request
        body = (
            '<div id="host"></div>'
            if mode == "shadow_region"
            else form
            if focused
            else '<p id="ready">Account ready</p>'
            + "".join(f"<button>Choice {i}</button>" for i in range(300))
            + '<button id="target">Open activity</button>'
        )
        script = (
            "const root = document.querySelector('#host').attachShadow({mode:'open'});"
            + f"root.innerHTML = {form!r};"
            if mode == "shadow_region"
            else "const root = document;"
        )
        return html(
            body + '<p id="receipt">Pending</p>',
            script=script
            + """
            root.querySelector('#target').onclick = async () => {
                await fetch('/effect', {method:'POST'});
                document.querySelector('#receipt').textContent = 'Report ready';
            };
        """,
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with browsing(
        [
            Route("/account", home),
            Route("/other", home),
            Route("/effect", effect, methods=["POST"]),
        ]
    ) as (site, runtime):
        catalog = recheck_catalog()
        recipe = catalog["recipes"][0]
        recipe["origin"] = site.origin
        recipe["steps"][0]["url"] = site.url("/account")
        recipe["steps"][1]["role"] = "button"
        if mode == "history":
            recipe["steps"][0]["postcondition"]["evidence"] = {
                "kind": "location",
                "url": site.url("/account"),
            }
        if focused:
            recipe["steps"][1]["target_region"] = {
                "kind": "region",
                "region_kind": "form",
                "text": "Account ready Account activity Open activity",
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
            expansions = 0

            async def navigate(self, url: str) -> BrowserObservation:
                return await runtime.navigate(url)

            async def observe(self) -> BrowserObservation:
                self.observation_count += 1
                result = await runtime.observe()
                if self.observation_count == 2:
                    if mode == "history":
                        await runtime._current_page().evaluate(
                            "history.pushState({}, '', '/other')"
                        )
                    elif mode in {
                        "changed",
                        "region_changed",
                        "failure",
                        "region_failure",
                        "conflict",
                    }:
                        text = (
                            "Account unavailable"
                            if "failure" in mode
                            else "Account ready\nAccount unavailable"
                            if mode == "conflict"
                            else "Signed out"
                        )
                        await runtime._current_page().evaluate(
                            "text => document.querySelector('#ready').innerText = text", text
                        )
                return result

            async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
                self.expansions += 1
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
        accepted = mode in {"continuation", "region", "shadow_region", "lost_response", "replaced"}
        async with bootstrap.build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(turns=[ScriptedTurn(text="Reported current evidence.")]),
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
            assert len(approvals) == int(accepted)
            assert not effects and not provider.actions
            assert provider.expansions == 1
            if mode == "replaced":
                await (
                    runtime._current_page()
                    .locator("#target")
                    .evaluate("""node => {
                        const replacement = node.cloneNode(true);
                        replacement.onclick = node.onclick;
                        node.replaceWith(replacement);
                    }""")
                )
            if approvals:
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run_id)
            assert saved is not None
            state = saved.working_state["browser_workflow"]
            succeeded = accepted and mode != "replaced"
            assert state["status"] == (
                "verified"
                if succeeded
                else "observed_failure"
                if "failure" in mode
                else "unverified"
            )
            assert state["completed_steps"] == (2 if succeeded else 1)
            assert state["target_search"] == {"step": 1, "windows": 2}
            calls = [item for item in saved.conversation if isinstance(item, ToolCallItem)]
            assert sum(call.name == "browser.navigate" for call in calls) == 1
            assert (
                sum(
                    call.arguments == {"wait_for": recipe["steps"][0]["postcondition"]}
                    for call in calls
                )
                == 2
            )
            assert saved.working_state["browser_workflow_report_only"]
        assert effects == ([True] if succeeded else [])
        assert len(provider.actions) == int(accepted)
        assert provider.expansions == 1
