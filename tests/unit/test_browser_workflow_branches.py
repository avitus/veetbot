"""Reviewed read branches cannot turn missing evidence into permission to act."""

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from agent_core.domain.browser_workflows import BrowserWorkflowRecipe
from tests.unit.test_browser_workflows import CATALOG


def branch_catalog() -> dict[str, Any]:
    catalog = deepcopy(CATALOG)
    catalog["recipes"][0]["steps"].insert(
        1,
        {
            "kind": "observe",
            "postcondition": {
                "evidence": {"kind": "text", "text": "Report ready"},
                "failure_evidence": {"kind": "text", "text": "Report closed"},
                "timeout_ms": 0,
            },
            "branches": {"satisfied": 3, "failed": 2},
        },
    )
    return catalog


def test_forward_read_branch_accepts_reviewed_positive_alternatives() -> None:
    recipe = BrowserWorkflowRecipe.model_validate(branch_catalog()["recipes"][0])
    assert recipe.steps[1].model_dump()["branches"] == {"satisfied": 3, "failed": 2}


@pytest.mark.parametrize(
    "text,status,completed",
    [
        ("Report ready", "verified", 2),
        ("Report closed", "observed_failure", 2),
        ("Pending", "unverified", 1),
        ("Report ready\nReport closed", "unverified", 1),
    ],
)
async def test_read_branch_completes_without_an_unnecessary_action(
    monkeypatch: Any,
    text: str,
    status: str,
    completed: int,
) -> None:
    import agent_core.bootstrap as bootstrap
    from agent_core.config import load_config_document, load_settings
    from agent_core.domain.browser import BrowserObservation
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.runs import RunStatus
    from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
    from tests.unit.test_browser_tools import FakeBrowserProvider
    from tests.unit.test_config import base_environment

    catalog = branch_catalog()
    catalog["recipes"][0]["steps"][1]["branches"]["failed"] = 3
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
        script=FakeModelScript(turns=[ScriptedTurn(text="Report was already ready.")]),
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
            app.principal,
            "general",
            {},
            browser_profile_id=PROFILE_ID,
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        assert not await app.approvals.list_pending(run_id=run_id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.working_state["browser_workflow"]
        assert state["completed_steps"] == completed
        if completed == 2:
            assert state["next_step"] == 3
        else:
            assert "next_step" not in state
        assert state["status"] == status
    assert not provider.actions


def test_legacy_recipe_definition_digest_is_unchanged() -> None:
    from agent_core.runtime.browser_workflows import _definition

    recipe = BrowserWorkflowRecipe.model_validate(CATALOG["recipes"][0])
    assert _definition(recipe) == "1ae53abe422e679ed43eeeade368c1381c805c8b3d4f1eff727a74f9d1cc0ff0"


def test_branch_destination_is_part_of_definition_identity() -> None:
    from agent_core.runtime.browser_workflows import _definition

    document = branch_catalog()["recipes"][0]
    first = _definition(BrowserWorkflowRecipe.model_validate(document))
    document["steps"][1]["branches"]["failed"] = 3
    assert _definition(BrowserWorkflowRecipe.model_validate(document)) != first


@pytest.mark.parametrize("target", [-1, 0, 1, 4, 17, True, 2.0, "2"])
@pytest.mark.parametrize("outcome", ["satisfied", "failed"])
def test_branch_rejects_non_forward_or_untyped_destinations(target: Any, outcome: str) -> None:
    document = branch_catalog()["recipes"][0]
    document["steps"][1]["branches"][outcome] = target
    with pytest.raises(ValidationError):
        BrowserWorkflowRecipe.model_validate(document)


@pytest.mark.parametrize(
    "change", ["missing_success", "missing_failure", "unknown", "no_predicate", "act", "navigate"]
)
def test_branch_is_closed_and_only_available_on_a_guarded_read(change: str) -> None:
    document = branch_catalog()["recipes"][0]
    step = document["steps"][1]
    if change == "missing_success":
        del step["postcondition"]["evidence"]
    elif change == "missing_failure":
        del step["postcondition"]["failure_evidence"]
    elif change == "unknown":
        step["branches"]["not_observed"] = 2
    elif change == "no_predicate":
        del step["postcondition"]
    else:
        document["steps"][2 if change == "act" else 0]["branches"] = step.pop("branches")
    with pytest.raises(ValidationError):
        BrowserWorkflowRecipe.model_validate(document)


@pytest.mark.parametrize("change", ["generation", "definition", "cancelled", "expired", "calls"])
async def test_selected_branch_cannot_bypass_resume_fences(monkeypatch: Any, change: str) -> None:
    import agent_core.bootstrap as bootstrap
    import agent_core.runtime.browser_workflows as workflows
    from agent_core.config import load_config_document, load_settings
    from agent_core.domain.approvals import ApprovalResolutionType
    from agent_core.domain.browser import BrowserObservation
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.runs import RunStatus
    from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
    from tests.unit.test_browser_tools import FakeBrowserProvider
    from tests.unit.test_config import base_environment

    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            branch_catalog()
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )

    class Provider(FakeBrowserProvider):
        def _observation(self, url: str) -> BrowserObservation:
            return super()._observation(url).model_copy(update={"text": "Report closed"})

    provider = Provider()
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
            app.principal,
            "general",
            {},
            browser_profile_id=PROFILE_ID,
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        assert len(approvals) == 1
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
            assert checkpoint is not None
            state = checkpoint.working_state["browser_workflow"]
            assert state["completed_steps"] == state["next_step"] == 2
            assert state["last_branch"] == {"step": 1, "status": "failed", "next_step": 2}
            if change == "generation":
                profile = await uow.browser_profiles.get(PROFILE_ID, app.principal)
                await uow.browser_profiles.advance_generation(
                    PROFILE_ID,
                    app.principal,
                    expected_generation=profile.generation,
                    updated_at=app.clock.now(),
                )
            elif change in {"expired", "calls"}:
                if change == "expired":
                    state["expires_at"] = app.clock.now().isoformat()
                else:
                    state["calls"] = 64
                checkpoint.version += 1
                await uow.checkpoints.write(run_id, checkpoint, full=True)
        if change == "definition":
            monkeypatch.setattr(workflows, "_definition", lambda _recipe: "changed")
        if change == "cancelled":
            await app.services.runs.cancel(app.principal, run_id)
        else:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.get(run_id)).status in {RunStatus.FAILED, RunStatus.CANCELLED}
        assert not provider.actions
