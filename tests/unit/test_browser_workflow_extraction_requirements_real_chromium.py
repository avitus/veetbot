"""Insufficient real collection evidence never admits later workflow effects."""

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


@pytest.mark.parametrize(
    "mode",
    [
        "complete",
        "shadow",
        "short",
        "empty",
        "omitted",
        "partial_allowed",
        "prior_effect",
        "lost_prior_effect",
    ],
)
async def test_real_extraction_requirements_bound_workflow_completion(
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
            + "root.innerHTML = '<table><caption>Report data</caption>"
            + "<tr><td>New</td></tr>"
            * (
                0
                if mode == "empty"
                else 1
                if mode in {"short", "prior_effect", "lost_prior_effect"}
                else 2
            )
            + "</table>';"
            + """
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
        prior = mode in {"prior_effect", "lost_prior_effect"}
        accepted = mode in {"complete", "shadow", "partial_allowed"}
        extraction = {
            **EXTRACT,
            "row_limit": 1 if mode in {"omitted", "partial_allowed"} else 2,
            "minimum_rows": 1 if mode in {"omitted", "partial_allowed"} else 2,
            "require_complete": mode != "partial_allowed",
        }
        recipe["steps"].insert(2 if prior else 1, extraction)
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
                return await runtime.navigate(url)

            async def observe(self) -> BrowserObservation:
                observed = await runtime.observe()
                self.reads.append(observed)
                return observed

            async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
                self.requests.append(request)
                self.extracted = await runtime.extract(request)
                return self.extracted

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                self.actions.append(action)
                result = await runtime.act(action, constraint=constraint)
                if mode == "lost_prior_effect":
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
            assert len(approvals) == int(prior or accepted)
            assert not effects
            if approvals:
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                checkpoint = await uow.checkpoints.latest(run_id)
            assert checkpoint is not None
            state = checkpoint.working_state["browser_workflow"]
            assert state["status"] == ("verified" if accepted else "insufficient_evidence")
            assert state["completed_steps"] == (3 if accepted else 2 if prior else 1)
            assert checkpoint.working_state["browser_workflow_report_only"]
            events = await app.runs.events(run_id)
            assert not any(event.event_type == "run.waiting_for_user" for event in events)
        assert effects == [True] * int(prior or accepted)
        assert len(provider.actions) == int(prior or accepted)
        assert len(provider.requests) == 1
        assert provider.extracted is not None and provider.extracted.extraction is not None
        result = provider.extracted.extraction
        assert result.status == "extracted"
        assert len(result.rows) == (
            0 if mode == "empty" else 2 if mode in {"complete", "shadow"} else 1
        )
        assert bool(result.omitted_rows) == (mode in {"omitted", "partial_allowed"})
