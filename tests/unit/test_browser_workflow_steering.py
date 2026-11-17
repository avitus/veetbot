"""Owner follow-ups retire a reviewed recipe at a safe tool boundary."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import agent_core.bootstrap as bootstrap
import agent_core.runtime.browser_workflows as workflows
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import BrowserObservation, BrowserProviderError
from agent_core.domain.browser_workflows import BrowserWorkflowCatalog
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedTurn,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import SubmitResult, TextContentBlock
from agent_core.runtime.loop import checkpoint as runtime_checkpoint
from agent_core.tools.validation import validate_and_normalize
from tests.unit.test_browser_composition import GRANT_NOW, PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment

FOLLOW_UP = "Stop opening the report and summarize what you have read."


@pytest.mark.parametrize("phase,calls,observations", [("read", 1, 0), ("before_action", 2, 1)])
async def test_follow_up_retires_the_recipe_before_its_next_tool(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    calls: int,
    observations: int,
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            CATALOG
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )
    provider = FakeBrowserProvider()
    async with bootstrap.build(
        settings=replace(
            load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            run_steering_enabled=True,
        ),
        script=FakeModelScript(turns=[ScriptedTurn(text="Stopped.", context_contains=FOLLOW_UP)]),
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
        original = runtime_checkpoint
        held: list[SubmitResult] = []

        async def checkpoint(context: Any, trigger: str) -> None:
            await original(context, trigger)
            if (
                trigger == "browser_workflow_progress"
                and context.checkpoint.working_state["browser_workflow"]["phase"] == phase
                and not held
            ):
                held.append(
                    await app.services.runs.submit(
                        app.principal, session.id, [TextContentBlock(text=FOLLOW_UP)], None, None
                    )
                )

        monkeypatch.setattr(workflows, "checkpoint", checkpoint)
        run_id = await app.runs.submit("Read my saved report.", session.id)
        assert not await app.approvals.list_pending(run_id=run_id), (
            "old recipe still proposed an action"
        )
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        assert [item.run_id for item in held] == [run_id]
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
            assert await uow.pending_inputs.list_for_run(run_id, app.principal) == []
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == "steered"
        assert state["completed_steps"] == 1
        assert state["calls"] == calls
        assert not saved.working_state.get("browser_workflow_report_only")
        events = await app.runs.events(run_id)
        follow_ups = [
            event
            for event in events
            if event.event_type == "user.message.created" and event.payload.get("pending_input_id")
        ]
        assert len(follow_ups) == 1
        pending = set()
        for item in saved.conversation:
            if isinstance(item, ToolCallItem):
                pending.add(item.call_id)
            elif isinstance(item, ToolResultItem):
                pending.discard(item.call_id)
            elif isinstance(item, UserMessage) and item.follow_up:
                assert not pending
    assert provider.observation_count == observations
    assert not provider.actions


@pytest.mark.parametrize("loss,receipt", [(False, True), (True, True), (True, False)])
async def test_follow_up_waits_for_approval_and_effect_reconciliation(
    monkeypatch: pytest.MonkeyPatch, loss: bool, receipt: bool
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            CATALOG
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )

    class Provider(FakeBrowserProvider):
        def _observation(self, url: str) -> BrowserObservation:
            return (
                super()
                ._observation(url)
                .model_copy(
                    update={"text": "Report ready" if self.actions and receipt else "Pending"}
                )
            )

        async def act(self, action: Any, *, constraint: Any = None) -> BrowserObservation:
            result = await super().act(action, constraint=constraint)
            if loss:
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
                ScriptedTurn(text="Checked.", context_contains=FOLLOW_UP if receipt else None),
                ScriptedTurn(text="Follow-up received."),
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
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        assert len(approvals) == 1 and not provider.actions
        held = await app.services.runs.submit(
            app.principal, session.id, [TextContentBlock(text=FOLLOW_UP)], None, None
        )
        assert held.run_id == run_id
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == ("steered" if receipt else "unverified")
        assert state["completed_steps"] == (2 if receipt else 1)
        assert bool(saved.working_state.get("browser_workflow_report_only")) is not receipt
        events = await app.runs.events(run_id)
        assert sum(
            event.event_type == "user.message.created"
            and bool(event.payload.get("pending_input_id"))
            for event in events
        ) == int(receipt)
    assert len(provider.actions) == 1
    assert provider.observation_count == (2 if loss else 1)


async def test_retired_recipe_cannot_restart_after_a_crash() -> None:
    runner = workflows.BrowserWorkflowRunner(
        BrowserWorkflowCatalog(),
        policy_version="changed",
        validate_arguments=validate_and_normalize,
    )
    context: Any = SimpleNamespace(
        checkpoint=SimpleNamespace(working_state={"browser_workflow": {"status": "steered"}})
    )
    assert await runner(context) is None


@pytest.mark.parametrize(
    "phase,loss", [("feed_extract", False), ("feed_scroll", False), ("feed_scroll", True)]
)
async def test_feed_handoff_retains_observed_posts_and_uncertain_scroll_fence(
    monkeypatch: pytest.MonkeyPatch, phase: str, loss: bool
) -> None:
    from tests.unit.test_browser_feed_workflow import (
        INTENT,
        FeedProvider,
        seed_feed_profile,
    )

    class Provider(FeedProvider):
        async def act(self, action: Any, *, constraint: Any = None) -> BrowserObservation:
            observed = await super().act(action, constraint=constraint)
            if loss:
                raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
            return observed

    provider = Provider()
    async with bootstrap.build(
        settings=replace(
            load_settings(
                {
                    **base_environment(),
                    "SANDBOX_MECHANISM": "fake",
                    "BROWSER_FEED_WORKFLOW_ENABLED": "1",
                }
            ),
            run_steering_enabled=True,
        ),
        fixed_clock_at=GRANT_NOW,
        script=FakeModelScript(
            turns=[
                ScriptedTurn(text="Observed feed.", context_contains=None if loss else FOLLOW_UP),
                ScriptedTurn(text="Follow-up received."),
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
        await seed_feed_profile(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        original = runtime_checkpoint
        held: list[SubmitResult] = []

        async def checkpoint(context: Any, trigger: str) -> None:
            await original(context, trigger)
            if (
                trigger == "browser_workflow_progress"
                and context.checkpoint.working_state["browser_workflow"]["phase"] == phase
                and not held
            ):
                held.append(
                    await app.services.runs.submit(
                        app.principal, session.id, [TextContentBlock(text=FOLLOW_UP)], None, None
                    )
                )

        monkeypatch.setattr(workflows, "checkpoint", checkpoint)
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == ("uncertain_scroll" if loss else "steered")
        assert len(state["feed"]["post_ids"]) == (3 if phase == "feed_extract" else 6)
        assert bool(saved.working_state.get("browser_workflow_report_only")) is loss
        assert [item.run_id for item in held] == [run_id]
    assert len(provider.actions) == int(phase == "feed_scroll")
