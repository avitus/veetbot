"""A follow-up never races or repeats an approved browser effect."""

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import agent_core.bootstrap as bootstrap
import agent_core.runtime.browser_workflows as workflows
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserObservation,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import SubmitResult, TextContentBlock
from agent_core.runtime.loop import checkpoint as runtime_checkpoint
from tests.unit.test_browser_composition import PROFILE_ID
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflow_steering import FOLLOW_UP
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


@pytest.mark.parametrize("mode", ["before_action", "approved", "lost_response", "unverified"])
async def test_real_workflow_handoff_preserves_exactly_the_sent_effect(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="open">Open activity</button><p id="status">Pending</p>',
            script="""document.querySelector('#open').onclick = async () => {
              await fetch('/effect', {method:'POST'});
              document.querySelector('#status').textContent = 'Report ready';
            };"""
            if mode != "unverified"
            else """document.querySelector('#open').onclick = async () => {
              await fetch('/effect', {method:'POST'});
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

            async def navigate(self, url: str) -> BrowserObservation:
                return await runtime.navigate(url)

            async def observe(self) -> BrowserObservation:
                return await runtime.observe()

            async def act(self, action: Any, *, constraint: Any = None) -> BrowserObservation:
                self.actions.append(action)
                result = await runtime.act(action, constraint=constraint)
                if mode in {"lost_response", "unverified"}:
                    raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
                return result

        provider = Provider()
        async with bootstrap.build(
            settings=replace(
                load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
                run_steering_enabled=True,
            ),
            script=FakeModelScript(
                turns=[
                    ScriptedTurn(
                        text="Checked.",
                        context_contains=FOLLOW_UP if mode != "unverified" else None,
                    ),
                    ScriptedTurn(text="Received follow-up."),
                ]
            ),
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
            original = runtime_checkpoint
            held: list[SubmitResult] = []

            async def follow_up() -> None:
                held.append(
                    await app.services.runs.submit(
                        app.principal, session.id, [TextContentBlock(text=FOLLOW_UP)], None, None
                    )
                )

            async def checkpoint(context: Any, trigger: str) -> None:
                await original(context, trigger)
                if (
                    mode == "before_action"
                    and trigger == "browser_workflow_progress"
                    and context.checkpoint.working_state["browser_workflow"]["phase"]
                    == "before_action"
                    and not held
                ):
                    await follow_up()

            monkeypatch.setattr(workflows, "checkpoint", checkpoint)
            run_id = await app.runs.submit("Read my saved report.", session.id)
            approvals = await app.approvals.list_pending(run_id=run_id)
            assert len(approvals) == int(mode != "before_action")
            assert not effects
            if approvals:
                await follow_up()
                assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
                await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            assert [result.run_id for result in held] == [run_id]
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run_id)
            assert saved is not None
            assert saved.working_state["browser_workflow"]["status"] == (
                "unverified" if mode == "unverified" else "steered"
            )
            events = await app.runs.events(run_id)
            assert sum(
                event.event_type == "user.message.created"
                and bool(event.payload.get("pending_input_id"))
                for event in events
            ) == int(mode != "unverified")
        assert effects == [True] * int(mode != "before_action")
        assert len(provider.actions) == len(effects)
