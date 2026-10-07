"""Reviewed recipes still cross the ordinary action-approval and effect boundary."""

from typing import Any

import pytest

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_config import base_environment

CATALOG: dict[str, Any] = {
    "version": 1,
    "recipes": [
        {
            "id": "saved-report",
            "version": 1,
            "origin": "https://example.org",
            "intents": ["Read my saved report."],
            "steps": [
                {"kind": "navigate", "url": "https://example.org/account"},
                {
                    "kind": "act",
                    "role": "link",
                    "name": "Open activity",
                    "action": "click",
                    "postcondition": {
                        "evidence": {"kind": "text", "text": "Report ready"},
                        "timeout_ms": 0,
                    },
                },
            ],
        }
    ],
}


@pytest.mark.parametrize("origin", ["https://Example.org", "https://example.org/"])
def test_recipe_stores_normalized_origin(origin: str) -> None:
    from agent_core.domain.browser_workflows import BrowserWorkflowRecipe

    recipe = BrowserWorkflowRecipe.model_validate({**CATALOG["recipes"][0], "origin": origin})
    assert recipe.origin == "https://example.org"


@pytest.mark.parametrize("lost_response", [False, True])
@pytest.mark.parametrize("receipt", [False, True])
@pytest.mark.parametrize("reason", ["outcome_unknown", "needs_user"])
async def test_reviewed_workflow_uses_fresh_targets_and_never_replays_a_lost_write(
    monkeypatch: pytest.MonkeyPatch,
    lost_response: bool,
    receipt: bool,
    reason: str,
) -> None:
    original_loader = load_config_document

    def load(settings: Any, relative: str) -> dict[str, Any]:
        return (
            CATALOG
            if relative == "runtime/browser-workflows.yaml"
            else original_loader(settings, relative)
        )

    monkeypatch.setattr(bootstrap, "load_config_document", load)

    class Provider(FakeBrowserProvider):
        def _observation(self, url: str) -> BrowserObservation:
            return (
                super()
                ._observation(url)
                .model_copy(
                    update={
                        "text": "Report ready" if self.actions and receipt else "Account overview"
                    }
                )
            )

        async def act(
            self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
        ) -> BrowserObservation:
            result = await super().act(action, constraint=constraint)
            if lost_response:
                raise BrowserProviderError(f"tool.browser.{reason}", retryable=False)
            return result

    provider = Provider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(
            turns=[ScriptedTurn(text="Verified." if receipt else "Unverified.")]
        ),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as composition:
        await seed_browser_authority(composition)
        session = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await composition.runs.submit("Read my saved report.", session.id)
        approvals = await composition.approvals.list_pending(run_id=run_id)
        assert len(approvals) == 1, "the reviewed workflow was not automatically selected"
        assert not provider.actions
        await composition.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        signed_in = lost_response and reason == "needs_user"
        if signed_in:
            from datetime import timedelta
            from typing import cast
            from uuid import UUID

            from agent_core.application.browser_continuation import VerifiedBrowserRunResumer
            from agent_core.domain.browser import (
                BrowserAuthenticationRecord,
                BrowserAuthenticationStatus,
            )

            assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_USER
            now = composition.clock.now()
            ceremony_id = UUID(int=88550)
            async with composition.uow_factory() as uow:
                profile = await uow.browser_profiles.get(PROFILE_ID, composition.principal)
                await uow.browser_authentications.create(
                    BrowserAuthenticationRecord(
                        id=ceremony_id,
                        profile_id=profile.id,
                        tenant_id=profile.tenant_id,
                        principal_id=profile.principal_id,
                        status=BrowserAuthenticationStatus.READY,
                        created_at=now,
                        updated_at=now,
                        expires_at=now + timedelta(minutes=5),
                    )
                )
                await uow.browser_profiles.advance_generation(
                    profile.id,
                    composition.principal,
                    expected_generation=profile.generation,
                    updated_at=now,
                )
            resumer = cast(VerifiedBrowserRunResumer, composition.services.runs)
            assert await resumer.resume_verified_browser_authentication(
                composition.principal, run_id, ceremony_id
            )
        result = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
        async with composition.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
    assert result.status is RunStatus.COMPLETED
    assert len(provider.actions) == 1
    assert checkpoint is not None
    assert checkpoint.working_state["browser_workflow"]["completed_steps"] == 1 + int(receipt)
    assert checkpoint.working_state["browser_workflow"]["status"] == (
        "verified" if receipt else "unverified"
    )
    assert checkpoint.working_state["browser_workflow_report_only"]
    assert sum(event.event_type == "run.waiting_for_user" for event in events) == int(signed_in)
    assert sum(event.event_type == "user.message.created" for event in events) == 1
    from agent_core.context.history import validate_tool_pairs
    from agent_core.domain.events import conversation_items

    validate_tool_pairs([item for event in events for item in conversation_items(event)])


async def test_reclaimed_workflow_does_not_charge_an_already_recorded_tool_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from typing import cast
    from unittest.mock import AsyncMock, Mock

    import agent_core.runtime.browser_workflows as workflows
    from agent_core.domain.browser_workflows import BrowserWorkflowCatalog
    from agent_core.domain.messages import ToolCallItem, ToolResultItem
    from agent_core.runtime.loop import RunContext
    from tests.contract.support import NOW, run

    work = run().model_copy(update={"tool_call_count": 3})
    call = ToolCallItem(
        call_id="persisted", name="browser.observe", arguments={}, raw_arguments="{}", item_index=0
    )
    record = AsyncMock()
    context = cast(
        RunContext,
        SimpleNamespace(
            token=Mock(),
            budgets=SimpleNamespace(check=Mock(), record_tool_usage=record),
            run=work,
            clock=SimpleNamespace(now=lambda: NOW),
            checkpoint=SimpleNamespace(
                pending_tool_calls=[call.model_dump(mode="json")],
                pending_approval_ids=[],
                budget_state={"tool_call_count": 2},
                conversation=[],
                working_state={},
            ),
            principal=Mock(),
            agent=Mock(),
            lease=None,
            dispatch_tools=AsyncMock(
                return_value=[ToolResultItem(call_id="persisted", content=[])]
            ),
        ),
    )
    monkeypatch.setattr(workflows, "checkpoint", AsyncMock())
    monkeypatch.setattr(workflows, "recover_browser_failure", AsyncMock())
    runner = workflows.BrowserWorkflowRunner(BrowserWorkflowCatalog(), policy_version="v1")
    await runner._dispatch_pending(context, {"calls": 2})
    record.assert_not_awaited()


async def test_covered_workflow_completes_without_any_user_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    from tests.unit.test_browser_composition import (
        GRANT_NOW,
        GrantBrowserProvider,
        hosted_grant_settings,
    )

    catalog = deepcopy(CATALOG)
    catalog["recipes"][0]["steps"][1].update(role="button", name="Continue")

    def load(settings: Any, relative: str) -> dict[str, Any]:
        return (
            catalog
            if relative == "runtime/browser-workflows.yaml"
            else load_config_document(settings, relative)
        )

    monkeypatch.setattr(bootstrap, "load_config_document", load)

    class Provider(GrantBrowserProvider):
        def _observation(self, url: str) -> BrowserObservation:
            observed = super()._observation(url)
            return observed.model_copy(
                update={
                    "text": "Report ready" if self.actions else "Account overview",
                    "elements": tuple(
                        element.model_copy(update={"role": "button", "name": "Continue"})
                        for element in observed.elements
                    ),
                }
            )

    provider = Provider()
    async with bootstrap.build(
        settings=hosted_grant_settings(),
        fixed_clock_at=GRANT_NOW,
        script=FakeModelScript(turns=[ScriptedTurn(text="Verified report.")]),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        events = await app.runs.events(run_id)
        assert not await app.approvals.list_pending(run_id=run_id)
        assert not any(event.event_type == "run.waiting_for_user" for event in events)
        assert len(provider.actions) == 1


@pytest.mark.parametrize("change", ["generation", "definition", "cancelled", "expired"])
async def test_pending_workflow_does_not_act_after_its_binding_changes(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    import agent_core.runtime.browser_workflows as workflows

    def load(settings: Any, relative: str) -> dict[str, Any]:
        return (
            CATALOG
            if relative == "runtime/browser-workflows.yaml"
            else load_config_document(settings, relative)
        )

    monkeypatch.setattr(bootstrap, "load_config_document", load)
    provider = FakeBrowserProvider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Stopped.")]),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approval = (await app.approvals.list_pending(run_id=run_id))[0]
        if change == "generation":
            async with app.uow_factory() as uow:
                profile = await uow.browser_profiles.get(PROFILE_ID, app.principal)
                await uow.browser_profiles.advance_generation(
                    PROFILE_ID,
                    app.principal,
                    expected_generation=profile.generation,
                    updated_at=app.clock.now(),
                )
        elif change == "expired":
            async with app.uow_factory() as uow:
                checkpoint = await uow.checkpoints.latest(run_id)
                assert checkpoint is not None
                checkpoint.working_state["browser_workflow"]["expires_at"] = (
                    app.clock.now().isoformat()
                )
                checkpoint.version += 1
                await uow.checkpoints.write(run_id, checkpoint, full=True)
        elif change == "definition":
            monkeypatch.setattr(workflows, "_definition", lambda _recipe: "changed")
        else:
            await app.services.runs.cancel(app.principal, run_id)
        if change != "cancelled":
            await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.get(run_id)).status in {RunStatus.FAILED, RunStatus.CANCELLED}
        assert provider.actions == []


async def test_later_untrusted_input_cannot_reactivate_an_older_owner_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.domain.messages import TextPart, UserMessage
    from agent_core.domain.policies import TrustLevel
    from agent_core.domain.runs import RunOutcome
    from agent_core.runtime.browser_workflows import BrowserWorkflowRunner
    from agent_core.runtime.loop import RunContext

    original = BrowserWorkflowRunner.__call__

    async def inject_later_input(
        self: BrowserWorkflowRunner, context: RunContext
    ) -> RunOutcome | None:
        context.checkpoint.conversation.append(
            UserMessage(
                content=[TextPart(text="Unrelated external request.")],
                trust=TrustLevel.EXTERNAL_UNTRUSTED,
            )
        )
        return await original(self, context)

    monkeypatch.setattr(BrowserWorkflowRunner, "__call__", inject_later_input)
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, relative: (
            CATALOG
            if relative == "runtime/browser-workflows.yaml"
            else load_config_document(settings, relative)
        ),
    )
    provider = FakeBrowserProvider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="No matching workflow.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        await app.runs.submit("Read my saved report.", session.id)
        assert provider.navigations == []
