"""Navigation completion requires a separate bounded read before later workflow steps."""

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import BrowserObservation
from agent_core.domain.browser_workflows import BrowserWorkflowRecipe, BrowserWorkflowStep
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, ToolCallItem
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


def navigation_catalog() -> dict[str, Any]:
    catalog = deepcopy(CATALOG)
    catalog["recipes"][0]["steps"][0]["postcondition"] = {
        "evidence": {"kind": "text", "text": "Account ready"},
        "failure_evidence": {"kind": "text", "text": "Account unavailable"},
        "timeout_ms": 0,
    }
    return catalog


def test_navigation_accepts_a_bounded_completion_condition() -> None:
    recipe = BrowserWorkflowRecipe.model_validate(navigation_catalog()["recipes"][0])
    assert recipe.steps[0].postcondition is not None
    assert recipe.steps[0].postcondition.timeout_ms == 0


@pytest.mark.parametrize(
    "condition", [{}, {"timeout_ms": 5001}, {"selector": "#ready"}, {"timeout_ms": -1}]
)
def test_navigation_keeps_the_existing_closed_condition_bounds(condition: dict[str, Any]) -> None:
    step = deepcopy(CATALOG["recipes"][0]["steps"][0])
    if condition:
        condition = {"role": "button", "name": "Ready", **condition}
    with pytest.raises(ValidationError):
        BrowserWorkflowStep.model_validate({**step, "postcondition": condition})


def test_navigation_condition_changes_definition_identity() -> None:
    from agent_core.runtime.browser_workflows import _definition

    recipe = BrowserWorkflowRecipe.model_validate(navigation_catalog()["recipes"][0])
    legacy = BrowserWorkflowRecipe.model_validate(CATALOG["recipes"][0])
    assert _definition(recipe) != _definition(legacy)
    assert _definition(legacy) == "1ae53abe422e679ed43eeeade368c1381c805c8b3d4f1eff727a74f9d1cc0ff0"


def test_capability_preflight_checks_navigation_condition_literals() -> None:
    from agent_core.runtime.browser_workflows import _supports_recipe
    from agent_core.tools.validation import validate_and_normalize
    from tests.unit.test_browser_workflow_capabilities import NAMES, SPECS

    recipe = BrowserWorkflowRecipe.model_validate(navigation_catalog()["recipes"][0])
    # Postcondition text is supported; this pin cannot express the new ready marker.
    schema = deepcopy(SPECS[1].input_schema)
    schema["allOf"] = [
        {
            "not": {
                "properties": {
                    "wait_for": {
                        "properties": {
                            "evidence": {
                                "properties": {"text": {"const": "Account ready"}},
                                "required": ["text"],
                            }
                        },
                        "required": ["evidence"],
                    }
                },
                "required": ["wait_for"],
            }
        }
    ]
    assert _supports_recipe(recipe, NAMES, SPECS, validate_arguments=validate_and_normalize)
    assert not _supports_recipe(
        recipe,
        NAMES,
        (SPECS[0], SPECS[1].model_copy(update={"input_schema": schema}), SPECS[2]),
        validate_arguments=validate_and_normalize,
    )


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Account ready", "verified"),
        ("Pending", "unverified"),
        ("Account unavailable", "observed_failure"),
        ("Account ready\nAccount unavailable", "unverified"),
    ],
)
async def test_only_verified_navigation_reaches_fresh_target_and_approval(
    monkeypatch: pytest.MonkeyPatch, text: str, status: str
) -> None:
    catalog = navigation_catalog()
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
        def _observation(self, url: str) -> BrowserObservation:
            return (
                super()
                ._observation(url)
                .model_copy(update={"text": "Report ready" if self.actions else text})
            )

    provider = Provider()
    ready = status == "verified"
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported observed progress.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        assert len(approvals) == int(ready)
        assert not provider.actions
        if approvals:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == status
        assert state["completed_steps"] == (2 if ready else 0)
        calls = [item for item in saved.conversation if isinstance(item, ToolCallItem)]
        assert calls[1].name == "browser.observe"
        assert calls[1].arguments == {
            "wait_for": catalog["recipes"][0]["steps"][0]["postcondition"]
        }
        assert [call.name for call in calls] == ["browser.navigate", "browser.observe"] + (
            ["browser.observe", "browser.act"] if ready else []
        )
        if ready:
            # The target read repeats the destination wait, so the action's
            # revision is one where the reviewed evidence held.
            assert calls[2].arguments == calls[1].arguments
            assert state["target_search"]["windows"] == 1
        assert saved.working_state["browser_workflow_report_only"]
    assert len(provider.actions) == int(ready)


def test_navigation_only_recipe_requires_pinned_wait_capability() -> None:
    from agent_core.runtime.browser_workflows import _supports_recipe
    from agent_core.tools.validation import validate_and_normalize
    from tests.unit.test_browser_workflow_capabilities import SPECS

    document = navigation_catalog()["recipes"][0]
    document["steps"] = document["steps"][:1]
    recipe = BrowserWorkflowRecipe.model_validate(document)
    assert not _supports_recipe(
        recipe, {"browser.navigate"}, SPECS[:1], validate_arguments=validate_and_normalize
    )
    assert _supports_recipe(
        recipe,
        {"browser.navigate", "browser.observe"},
        SPECS[:2],
        validate_arguments=validate_and_normalize,
    )


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("redirected", [False, True])
async def test_navigation_verification_sign_in_resumes_only_unfinished_read_work(
    monkeypatch: pytest.MonkeyPatch, resume: bool, redirected: bool
) -> None:
    from datetime import timedelta
    from typing import cast
    from uuid import UUID

    from agent_core.application.browser_continuation import VerifiedBrowserRunResumer
    from agent_core.domain.browser import (
        BrowserAuthenticationRecord,
        BrowserAuthenticationStatus,
        BrowserProviderError,
    )

    catalog = navigation_catalog()
    # A verified earlier effect must not be repeated after sign-in during a
    # later navigation's verification.
    steps = catalog["recipes"][0]["steps"]
    navigation = steps.pop(0)
    steps.insert(0, {"kind": "navigate", "url": navigation["url"]})
    steps.append(navigation)
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
        interrupted = False

        def _observation(self, url: str) -> BrowserObservation:
            if redirected and len(self.navigations) == 2:
                url = "https://example.org/redirected"
            return (
                super()._observation(url).model_copy(update={"text": "Report ready\nAccount ready"})
            )

        async def observe(self) -> BrowserObservation:
            if len(self.navigations) == 2 and not self.interrupted:
                self.interrupted = True
                raise BrowserProviderError("tool.browser.needs_user", retryable=False)
            return await super().observe()

    provider = Provider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported verified progress.")]),
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
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_USER
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        assert saved.working_state["browser_workflow"]["phase"] == "verify_navigation"
        assert saved.working_state["browser_workflow"]["completed_steps"] == 2
        assert len(provider.actions) == 1
        if not resume:
            await app.services.runs.cancel(app.principal, run_id)
            assert (await app.runs.get(run_id)).status is RunStatus.CANCELLED
            assert len(provider.navigations) == 2
        else:
            now = app.clock.now()
            ceremony_id = UUID(int=88551)
            async with app.uow_factory() as uow:
                profile = await uow.browser_profiles.get(PROFILE_ID, app.principal)
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
                    app.principal,
                    expected_generation=profile.generation,
                    updated_at=now,
                )
            assert await cast(
                VerifiedBrowserRunResumer, app.services.runs
            ).resume_verified_browser_authentication(app.principal, run_id, ceremony_id)
            assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run_id)
            assert saved is not None
            state = saved.working_state["browser_workflow"]
            assert state["status"] == "verified"
            assert state["completed_steps"] == 3
            assert len(provider.navigations) == (4 if redirected else 3)
            events = await app.runs.events(run_id)
            assert sum(event.event_type == "run.waiting_for_user" for event in events) == 1
            assert sum(event.event_type == "user.message.created" for event in events) == 1
        assert len(provider.actions) == 1


@pytest.mark.parametrize("failure", ["unavailable", "foreign", "expired", "calls"])
async def test_navigation_verification_preserves_read_and_admission_boundaries(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import agent_core.runtime.browser_workflows as workflows
    from agent_core.domain.browser import BrowserProviderError

    catalog = navigation_catalog()
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            catalog
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )
    original = workflows.BrowserWorkflowRunner._queue

    async def queue(
        self: Any, context: Any, state: Any, phase: str, name: str, arguments: Any
    ) -> None:
        if phase == "verify_navigation":
            if failure == "expired":
                state["expires_at"] = context.clock.now().isoformat()
            elif failure == "calls":
                state["calls"] = 64
        await original(self, context, state, phase, name, arguments)

    monkeypatch.setattr(workflows.BrowserWorkflowRunner, "_queue", queue)

    class Provider(FakeBrowserProvider):
        async def observe(self) -> BrowserObservation:
            if failure == "unavailable":
                raise BrowserProviderError("tool.browser.provider_unavailable", retryable=False)
            return (await super().observe()).model_copy(
                update={"url": "https://elsewhere.org/account", "text": "Account ready"}
            )

    provider = Provider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported incomplete navigation.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        assert not await app.approvals.list_pending(run_id=run_id)
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == (
            "bounded_stop" if failure in {"expired", "calls"} else "unverified"
        )
        assert state["completed_steps"] == 0
        assert provider.navigations == ["https://example.org/account"]
        assert not provider.actions
        if failure in {"expired", "calls"}:
            assert provider.observation_count == 0


@pytest.mark.parametrize("shape", ["control", "readiness", "region"])
@pytest.mark.parametrize(
    ("text", "status"),
    [("Overview", "unverified"), ("Overview\nAccount unavailable", "observed_failure")],
)
async def test_action_target_read_still_requires_the_verified_destination(
    monkeypatch: pytest.MonkeyPatch, shape: str, text: str, status: str
) -> None:
    """A same-origin page change after verification cannot supply an action target."""
    catalog = navigation_catalog()
    destination = catalog["recipes"][0]["steps"][0]["postcondition"]
    action = catalog["recipes"][0]["steps"][1]
    ready = {"evidence": {"kind": "text", "text": "Overview"}, "timeout_ms": 0}
    if shape == "readiness":
        action["precondition"] = ready
    elif shape == "region":
        action["target_region"] = {"kind": "region", "region_kind": "dialog", "text": "Confirm"}
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
        async def observe(self) -> BrowserObservation:
            observed = await super().observe()
            if self.observation_count == 1:
                return observed.model_copy(update={"text": "Overview\nAccount ready"})
            # The page moved within its origin and still offers the same control name.
            return observed.model_copy(update={"url": "https://example.org/other", "text": text})

    provider = Provider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported the changed page.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
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
        # The navigation was verified; the action never started.
        assert state["completed_steps"] == 1
        calls = [item for item in saved.conversation if isinstance(item, ToolCallItem)]
        assert [call.name for call in calls] == ["browser.navigate"] + ["browser.observe"] * (
            3 if shape == "readiness" else 2
        )
        # The target read asks for the destination again, in the same observation.
        assert calls[-1].arguments == {"wait_for": destination}
        assert state["target_search"] == {"step": 1, "windows": 1}
    assert not provider.actions


def test_destination_proof_applies_only_directly_after_its_executed_navigation() -> None:
    from agent_core.runtime.browser_workflows import _destination

    document = navigation_catalog()["recipes"][0]
    verified, action = document["steps"]
    read = {
        "kind": "observe",
        "postcondition": verified["postcondition"],
        "branches": {"satisfied": 3, "failed": 2},
    }
    document["steps"] = [{"kind": "navigate", "url": verified["url"]}, read, verified, action]
    recipe = BrowserWorkflowRecipe.model_validate(document)
    wait = {"wait_for": verified["postcondition"]}
    assert _destination(recipe, {"next_step": 3, "last_branch": {"next_step": 2}}, 3) == wait
    # A branch that skipped the navigation has no verified destination to keep.
    assert _destination(recipe, {"next_step": 3, "last_branch": {"next_step": 3}}, 3) is None
    # An unconditioned navigation and a non-navigation predecessor declare none.
    assert _destination(recipe, {}, 1) is None
    assert _destination(recipe, {"next_step": 2, "last_branch": {"next_step": 2}}, 2) is None


@pytest.mark.parametrize(
    ("steps", "text", "status", "completed"),
    [
        (1, "Account ready", "verified", 1),
        (1, "Pending", "unverified", 0),
        (2, "Account ready", "bounded_stop", 1),
    ],
)
@pytest.mark.parametrize("bound", ["tool_calls", "steps", "workflow_calls", "expired"])
async def test_a_reached_bound_stops_new_calls_but_not_a_completed_verification(
    monkeypatch: pytest.MonkeyPatch, bound: str, steps: int, text: str, status: str, completed: int
) -> None:
    import agent_core.runtime.browser_workflows as workflows
    from agent_core.domain.runs import RunLimits

    original = workflows.BrowserWorkflowRunner._queue

    async def queue(
        self: Any, context: Any, state: Any, phase: str, name: str, arguments: Any
    ) -> None:
        # Each bound is reached by the verification read, the second call.
        if phase == "verify_navigation" and bound == "workflow_calls":
            state["calls"] = 63
        await original(self, context, state, phase, name, arguments)
        if phase == "verify_navigation" and bound == "expired":
            state["expires_at"] = context.clock.now().isoformat()

    monkeypatch.setattr(workflows.BrowserWorkflowRunner, "_queue", queue)
    limits = {"tool_calls": RunLimits(max_tool_calls=2), "steps": RunLimits(max_steps=3)}
    catalog = navigation_catalog()
    catalog["recipes"][0]["steps"] = catalog["recipes"][0]["steps"][:steps]
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
        def _observation(self, url: str) -> BrowserObservation:
            return super()._observation(url).model_copy(update={"text": text})

    provider = Provider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported observed progress.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
        limits=limits.get(bound),
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        run = await app.runs.wait_terminal(run_id)
        assert run.status is RunStatus.COMPLETED
        # The bound admitted the navigation and its verification, and nothing else.
        assert run.tool_call_count == 2
        assert not await app.approvals.list_pending(run_id=run_id)
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == status
        assert state["completed_steps"] == completed
        calls = [item for item in saved.conversation if isinstance(item, ToolCallItem)]
        assert [call.name for call in calls] == ["browser.navigate", "browser.observe"]
    assert provider.observation_count == 1 and not provider.actions


async def test_an_action_on_the_last_permitted_call_is_neither_reconciled_nor_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.domain.runs import RunLimits

    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            CATALOG
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )
    # The page never shows the action's receipt, so its result is inconclusive.
    provider = FakeBrowserProvider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported an unverified action.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
        limits=RunLimits(max_tool_calls=3),
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approval = (await app.approvals.list_pending(run_id=run_id))[0]
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await app.runs.wait_terminal(run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.tool_call_count == 3
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        # The limit refuses the reconciling read; the sent action stays unverified.
        assert (state["status"], state["phase"]) == ("bounded_stop", "action")
        assert state["completed_steps"] == 1
        assert saved.working_state["browser_workflow_report_only"]
    assert len(provider.actions) == 1 and provider.observation_count == 1
