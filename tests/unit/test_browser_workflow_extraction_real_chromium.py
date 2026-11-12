"""Workflow extraction refreshes real page evidence without replaying prior writes."""

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
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflow_extraction import EXTRACT
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


@pytest.mark.parametrize("mode", ["read", "shadow", "action", "lost_response", "stale_extract"])
async def test_reviewed_extraction_uses_fresh_real_page_evidence(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="open">Open activity</button>'
            '<p id="status">Pending</p><div id="host"></div>',
            script="const host = document.querySelector('#host'); const root = "
            + ("host.attachShadow({mode:'open'});" if mode == "shadow" else "host;")
            + """
            root.innerHTML = '<table><tr><td>Old</td></tr></table>';
            document.querySelector('#open').onclick = async () => {
                await fetch('/effect', {method:'POST'});
                document.querySelector('#status').textContent = 'Report ready';
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
        recipe = catalog["recipes"][0]
        recipe["origin"] = site.origin
        recipe["steps"][0]["url"] = site.url("/account")
        recipe["steps"][1]["role"] = "button"
        has_action = mode in {"action", "lost_response"}
        if not has_action:
            recipe["steps"] = recipe["steps"][:1]
        recipe["steps"].append(EXTRACT)
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
                self.reads: list[BrowserObservation] = []
                self.requests: list[BrowserExtractionRequest] = []
                self.extracted: BrowserObservation | None = None

            async def navigate(self, url: str) -> BrowserObservation:
                previous = await runtime.navigate(url)
                await runtime._current_page().evaluate("""() => {
                    const host = document.querySelector('#host');
                    (host.shadowRoot || host).querySelector('td').textContent = 'New';
                }""")
                # Retire navigation's revision as another live read would.
                await runtime.observe()
                return previous

            async def observe(self) -> BrowserObservation:
                observed = await runtime.observe()
                self.reads.append(observed)
                return observed

            async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
                self.requests.append(request)
                if mode == "stale_extract":
                    await runtime.observe()
                self.extracted = await runtime.extract(request)
                return self.extracted

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                self.actions.append(action)
                result = await runtime.act(action, constraint=constraint)
                if mode == "lost_response":
                    raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
                return result

        provider = Provider()
        async with bootstrap.build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(turns=[ScriptedTurn(text="Checked the available rows.")]),
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
                app.principal, "general", {}, browser_profile_id=PROFILE_ID
            )
            run_id = await app.runs.submit("Read my saved report.", session.id)
            approvals = await app.approvals.list_pending(run_id=run_id)
            assert len(approvals) == int(has_action)
            assert not effects
            if approvals:
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                checkpoint = await uow.checkpoints.latest(run_id)
            assert checkpoint is not None
            state = checkpoint.working_state["browser_workflow"]
            assert state["status"] == ("unverified" if mode == "stale_extract" else "verified")
            assert state["completed_steps"] == (
                1 if mode == "stale_extract" else 2 + int(has_action)
            )
            assert checkpoint.working_state["browser_workflow_report_only"]
            events = await app.runs.events(run_id)
            assert not any(event.event_type == "run.waiting_for_user" for event in events)
        assert effects == [True] * int(has_action)
        assert len(provider.actions) == int(has_action)
        assert len(provider.requests) == 1
        source = provider.reads[-2] if mode == "stale_extract" else provider.reads[-1]
        assert provider.requests[0].expected_revision == source.revision
        if mode != "stale_extract":
            assert provider.extracted is not None and provider.extracted.extraction is not None
            assert provider.extracted.extraction.rows[0].cells[0].value == "New"
