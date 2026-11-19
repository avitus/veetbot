"""Reviewed action readiness is a bounded read, never action authorization."""

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


def readiness_catalog() -> dict[str, Any]:
    catalog = deepcopy(CATALOG)
    catalog["recipes"][0]["steps"][1]["precondition"] = {
        "evidence": {"kind": "text", "text": "Account ready"},
        "failure_evidence": {"kind": "text", "text": "Account unavailable"},
        "timeout_ms": 0,
    }
    return catalog


def test_reviewed_action_accepts_a_bounded_readiness_condition() -> None:
    recipe = BrowserWorkflowRecipe.model_validate(readiness_catalog()["recipes"][0])
    assert recipe.steps[1].precondition is not None
    assert recipe.steps[1].precondition.timeout_ms == 0


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "navigate", "url": "https://example.org/account"},
        {"kind": "observe"},
        {
            "kind": "extract",
            "collection_kind": "table",
            "fields": [{"name": "total", "column": 0, "type": "number"}],
        },
        {"kind": "collect_feed", "role": "button", "name": "Account"},
    ],
)
def test_readiness_is_only_valid_on_action_steps(step: dict[str, Any]) -> None:
    BrowserWorkflowStep.model_validate(step)
    with pytest.raises(ValidationError, match="workflow step mixes modes"):
        BrowserWorkflowStep.model_validate(
            {**step, "precondition": {"role": "button", "name": "Ready"}}
        )


@pytest.mark.parametrize(
    "condition", [{}, {"timeout_ms": 5001}, {"selector": "#ready"}, {"timeout_ms": -1}]
)
def test_readiness_keeps_the_existing_closed_condition_bounds(condition: dict[str, Any]) -> None:
    step = deepcopy(CATALOG["recipes"][0]["steps"][1])
    if condition:
        condition = {"role": "button", "name": "Ready", **condition}
    with pytest.raises(ValidationError):
        BrowserWorkflowStep.model_validate({**step, "precondition": condition})


def test_readiness_changes_definition_identity() -> None:
    from agent_core.runtime.browser_workflows import _definition

    recipe = BrowserWorkflowRecipe.model_validate(readiness_catalog()["recipes"][0])
    legacy = BrowserWorkflowRecipe.model_validate(CATALOG["recipes"][0])
    assert _definition(recipe) != _definition(legacy)
    assert _definition(legacy) == "1ae53abe422e679ed43eeeade368c1381c805c8b3d4f1eff727a74f9d1cc0ff0"


def test_capability_preflight_checks_readiness_literals() -> None:
    from agent_core.runtime.browser_workflows import _supports_recipe
    from agent_core.tools.validation import validate_and_normalize
    from tests.unit.test_browser_workflow_capabilities import NAMES, SPECS

    recipe = BrowserWorkflowRecipe.model_validate(readiness_catalog()["recipes"][0])
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
async def test_only_satisfied_readiness_reaches_fresh_target_and_approval(
    monkeypatch: pytest.MonkeyPatch, text: str, status: str
) -> None:
    catalog = readiness_catalog()
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
        assert state["completed_steps"] == (2 if ready else 1)
        calls = [item for item in saved.conversation if isinstance(item, ToolCallItem)]
        assert calls[1].name == "browser.observe"
        assert calls[1].arguments == {"wait_for": catalog["recipes"][0]["steps"][1]["precondition"]}
        assert [call.name for call in calls] == ["browser.navigate", "browser.observe"] + (
            ["browser.observe", "browser.act"] if ready else []
        )
        if ready:
            assert calls[2].arguments == {}
            assert state["target_search"]["windows"] == 1
        assert saved.working_state["browser_workflow_report_only"]
    assert len(provider.actions) == int(ready)
