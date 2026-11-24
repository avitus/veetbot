"""A page load is not verified navigation; redirects and delayed state need evidence."""

import pytest
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
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
from tests.unit.test_browser_workflow_navigation import navigation_catalog
from tests.unit.test_config import base_environment


@pytest.mark.parametrize(
    "mode",
    [
        "delayed",
        "redirect",
        "expected_redirect",
        "missing",
        "failure",
        "ambiguous",
        "lost_response",
        "moved",
    ],
)
async def test_real_navigation_verification_precedes_any_later_effect(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    effects: list[bool] = []

    async def entry(request: Request) -> Response:
        del request
        return RedirectResponse("/other" if mode == "redirect" else "/account")

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="open">Open activity</button><p id="ready">Pending</p>'
            '<p id="receipt">Pending</p>',
            script="""
            document.querySelector('#open').onclick = async () => {
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
            Route("/entry", entry),
            Route("/account", home),
            Route("/other", home),
            Route("/effect", effect, methods=["POST"]),
        ]
    ) as (site, runtime):
        catalog = navigation_catalog()
        recipe = catalog["recipes"][0]
        recipe["origin"] = site.origin
        recipe["steps"][0]["url"] = site.url("/entry")
        recipe["steps"][1]["role"] = "button"
        condition = recipe["steps"][0]["postcondition"]
        condition["timeout_ms"] = 1500
        if mode in {"redirect", "expected_redirect", "moved"}:
            condition["evidence"] = {"kind": "location", "url": site.url("/account")}
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
                result = await runtime.observe()
                if self.observation_count == 1 and mode not in {"missing", "redirect"}:
                    text = (
                        "Account unavailable"
                        if mode == "failure"
                        else "Account ready\nAccount unavailable"
                        if mode == "ambiguous"
                        else "Account ready"
                    )
                    # The first observation really is pending; the normal wait
                    # must poll the page after its visible state changes.
                    await runtime._current_page().evaluate(
                        "text => setTimeout(() => {"
                        "document.querySelector('#ready').innerText = text; }, 100)",
                        text,
                    )
                if self.observation_count == 1 and mode == "moved":
                    # The verified page then loads another same-origin page that
                    # offers the same control, before any target is read.
                    await runtime._current_page().goto(site.url("/other"))
                return result

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
            verified = mode in {"delayed", "expected_redirect", "lost_response"}
            assert len(approvals) == int(verified)
            assert not effects and not provider.actions
            if approvals:
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run_id)
            assert saved is not None
            state = saved.working_state["browser_workflow"]
            expected = (
                "verified"
                if verified
                else "observed_failure"
                if mode == "failure"
                else "unverified"
            )
            assert state["status"] == expected
            assert state["completed_steps"] == (2 if verified else 1 if mode == "moved" else 0)
            assert saved.working_state["browser_workflow_report_only"]
            calls = [item for item in saved.conversation if isinstance(item, ToolCallItem)]
            assert calls[1].arguments == {"wait_for": condition}
            # Verified once, then required again by the read that finds the target;
            # never repeated after the action, even when its response is lost.
            waits = [
                index
                for index, call in enumerate(calls)
                if call.arguments == {"wait_for": condition}
            ]
            assert waits == ([1, 2] if verified or mode == "moved" else [1])
            assert provider.navigations == [site.url("/entry")]
        assert effects == ([True] if verified else [])
        assert len(provider.actions) == int(verified)


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("after_dom", "target_not_observed"),
        ("after_history", "target_not_observed"),
        ("region_dom", "unverified"),
    ],
)
async def test_real_action_after_verified_navigation_reads_no_later_window(
    monkeypatch: pytest.MonkeyPatch, mode: str, status: str
) -> None:
    """A later window is a fresh capture: a changed document could supply its target."""
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        body = (
            '<p id="ready">Account ready</p><form><p>Account activity</p>'
            '<button type="button" id="target">Open activity</button></form>'
            if mode == "region_dom"
            # The target lies beyond the first control window.
            else '<p id="ready">Account ready</p>'
            + "".join(f"<button>Choice {i}</button>" for i in range(300))
            + '<button id="target">Open activity</button>'
        )
        return html(
            body,
            script="document.querySelector('#target').onclick = () => "
            "fetch('/effect', {method:'POST'});",
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with browsing(
        [Route("/account", home), Route("/other", home), Route("/effect", effect, methods=["POST"])]
    ) as (site, runtime):
        catalog = navigation_catalog()
        recipe = catalog["recipes"][0]
        recipe["origin"] = site.origin
        recipe["steps"][0]["url"] = site.url("/account")
        recipe["steps"][1]["role"] = "button"
        if mode == "after_history":
            recipe["steps"][0]["postcondition"]["evidence"] = {
                "kind": "location",
                "url": site.url("/account"),
            }
        if mode == "region_dom":
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
            expansions: int = 0

            async def navigate(self, url: str) -> BrowserObservation:
                return await runtime.navigate(url)

            async def observe(self) -> BrowserObservation:
                self.observation_count += 1
                result = await runtime.observe()
                if self.observation_count == 2:
                    # After the window that proved the destination, the same
                    # document changes before any later window could be read.
                    await runtime._current_page().evaluate(
                        "history.pushState({}, '', '/other')"
                        if mode == "after_history"
                        else "document.querySelector('#ready').textContent = 'Signed out'"
                    )
                return result

            async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
                self.expansions += 1
                return await runtime.expand(request)

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
            assert not await app.approvals.list_pending(run_id=run_id)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run_id)
            assert saved is not None
            state = saved.working_state["browser_workflow"]
            assert state["status"] == status
            assert state["completed_steps"] == 1
            assert saved.working_state["browser_workflow_report_only"]
            calls = [item for item in saved.conversation if isinstance(item, ToolCallItem)]
            wait = {"wait_for": recipe["steps"][0]["postcondition"]}
            assert [(call.name, call.arguments) for call in calls[1:]] == [
                ("browser.observe", wait),
                ("browser.observe", wait),
            ]
        assert provider.expansions == 0
    assert not effects and not provider.actions
