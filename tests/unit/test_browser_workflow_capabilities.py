"""Recipe selection must not start work that its pinned tools cannot express."""

from copy import deepcopy
from typing import Any

import pytest

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.browser_workflows import BrowserWorkflowRecipe
from agent_core.domain.errors import ToolValidationError
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.runtime.browser_workflows import _supports_recipe
from agent_core.tools.browser_act import ShadowBrowserActTool
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.browser_observe import CompletionBrowserObserveTool, PagedBrowserObserveTool
from agent_core.tools.validation import validate_and_normalize
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment

SPECS = (BrowserNavigateTool.spec, CompletionBrowserObserveTool.spec, ShadowBrowserActTool.spec)
NAMES = {spec.name for spec in SPECS}


def test_preflight_uses_injected_validator_without_executing_tools() -> None:
    recipe = BrowserWorkflowRecipe.model_validate(CATALOG["recipes"][0])
    checked: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def reject(arguments: dict[str, Any], schema: dict[str, Any]) -> object:
        checked.append((arguments, schema))
        raise ToolValidationError("unsupported pinned arguments")

    assert not _supports_recipe(recipe, NAMES, SPECS, validate_arguments=reject)
    assert checked == [({"url": recipe.steps[0].url}, SPECS[0].input_schema)]


@pytest.mark.parametrize(
    "mode", ["action", "focus", "wait", "postcondition", "extract", "branch", "feed", "feed_scroll"]
)
def test_preflight_validates_every_required_mode_against_pinned_schemas(mode: str) -> None:
    from tests.unit.test_browser_workflow_branches import branch_catalog
    from tests.unit.test_browser_workflow_regions import region_catalog

    catalog = (
        region_catalog()
        if mode == "focus"
        else branch_catalog()
        if mode == "branch"
        else deepcopy(CATALOG)
    )
    if mode.startswith("feed"):
        catalog = load_config_document(
            load_settings(base_environment()), "runtime/browser-workflows.yaml"
        )
    if mode == "extract":
        catalog["recipes"][0]["steps"][1] = {
            "kind": "extract",
            "collection_kind": "table",
            "fields": [{"name": "total", "column": 0, "type": "number"}],
        }
    recipe = BrowserWorkflowRecipe.model_validate(catalog["recipes"][0])
    assert _supports_recipe(recipe, NAMES, SPECS, validate_arguments=validate_and_normalize)
    name, field = {
        "action": ("browser.act", "kind"),
        "focus": ("browser.observe", "region_ref"),
        "wait": ("browser.observe", "wait_for"),
        "postcondition": ("browser.act", "postcondition"),
        "extract": ("browser.observe", "extract"),
        "branch": ("browser.act", "kind"),
        "feed": ("browser.observe", "extract"),
        "feed_scroll": ("browser.act", "delta_y"),
    }[mode]
    restricted = tuple(
        spec.model_copy(
            update={
                "input_schema": {
                    **deepcopy(spec.input_schema),
                    "allOf": [{"not": {"required": [field]}}],
                }
            }
        )
        if spec.name == name
        else spec
        for spec in SPECS
    )
    assert not _supports_recipe(
        recipe, NAMES, restricted, validate_arguments=validate_and_normalize
    )


def test_preflight_requires_both_advertisement_and_pinned_definition() -> None:
    recipe = BrowserWorkflowRecipe.model_validate(CATALOG["recipes"][0])
    assert not _supports_recipe(
        recipe, NAMES - {"browser.act"}, SPECS, validate_arguments=validate_and_normalize
    )
    assert not _supports_recipe(recipe, NAMES, SPECS[:2], validate_arguments=validate_and_normalize)
    assert not _supports_recipe(
        recipe,
        NAMES,
        (SPECS[0], PagedBrowserObserveTool.spec, SPECS[2]),
        validate_arguments=validate_and_normalize,
    )


@pytest.mark.parametrize("restriction", ["field_type", "row_limit"])
def test_extraction_checks_reviewed_literals_not_just_mode_presence(restriction: str) -> None:
    document = deepcopy(CATALOG["recipes"][0])
    document["steps"][1] = {
        "kind": "extract",
        "collection_kind": "table",
        "fields": [{"name": "total", "column": 0, "type": "number"}],
        "row_limit": 20,
    }
    recipe = BrowserWorkflowRecipe.model_validate(document)
    schema = deepcopy(SPECS[1].input_schema)
    if restriction == "field_type":
        schema["$defs"]["f"]["properties"]["type"] = {"const": "string"}
    else:
        schema["properties"]["extract"]["properties"]["row_limit"]["maximum"] = 10
    assert _supports_recipe(recipe, NAMES, SPECS, validate_arguments=validate_and_normalize)
    assert not _supports_recipe(
        recipe,
        NAMES,
        (SPECS[0], SPECS[1].model_copy(update={"input_schema": schema}), SPECS[2]),
        validate_arguments=validate_and_normalize,
    )


def test_navigate_only_recipe_does_not_require_unused_browser_tools() -> None:
    document = deepcopy(CATALOG["recipes"][0])
    document["steps"] = document["steps"][:1]
    assert _supports_recipe(
        BrowserWorkflowRecipe.model_validate(document),
        {"browser.navigate"},
        SPECS[:1],
        validate_arguments=validate_and_normalize,
    )


def test_wait_only_step_does_not_require_a_plain_observation() -> None:
    document = deepcopy(CATALOG["recipes"][0])
    document["steps"][1] = {
        "kind": "observe",
        "postcondition": document["steps"][1]["postcondition"],
    }
    observe = SPECS[1].model_copy(
        update={"input_schema": {**deepcopy(SPECS[1].input_schema), "required": ["wait_for"]}}
    )
    assert _supports_recipe(
        BrowserWorkflowRecipe.model_validate(document),
        NAMES,
        (SPECS[0], observe),
        validate_arguments=validate_and_normalize,
    )


@pytest.mark.parametrize("missing", ["browser.act", "browser.observe", "browser.navigate"])
async def test_incompatible_recipe_returns_to_planning_before_any_browser_call(
    monkeypatch: pytest.MonkeyPatch, missing: str
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
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Ordinary agent planning.")]),
        enabled_tools=[
            name
            for name in ("browser.navigate", "browser.observe", "browser.act")
            if name != missing
        ],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        assert not provider.navigations, "the incompatible recipe began partial execution"
        assert not provider.actions and provider.observation_count == 0
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        assert not await app.approvals.list_pending(run_id=run_id)
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        assert "browser_workflow" not in saved.working_state
        assert not saved.working_state.get("browser_workflow_report_only")


@pytest.mark.parametrize("mode", ["legacy", "deferred", "ambiguous"])
async def test_selection_uses_pinned_deferred_capabilities_without_resolving_ambiguity(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from agent_core.domain.approvals import ApprovalResolutionType
    from agent_core.domain.browser import BrowserObservation
    from agent_core.domain.runs import RunOutcome
    from agent_core.runtime.browser_workflows import BrowserWorkflowRunner
    from agent_core.runtime.loop import RunContext

    catalog = deepcopy(CATALOG)
    if mode == "ambiguous":
        other = deepcopy(catalog["recipes"][0])
        other["id"] = "other-report"
        catalog["recipes"][0]["steps"] = catalog["recipes"][0]["steps"][:1]
        catalog["recipes"].append(other)
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            catalog
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )
    original = BrowserWorkflowRunner.__call__

    async def pinned(self: BrowserWorkflowRunner, context: RunContext) -> RunOutcome | None:
        plan = context.context_plan
        if mode == "legacy":
            context.context_plan = plan.model_copy(
                update={
                    field: tuple(
                        PagedBrowserObserveTool.spec if spec.name == "browser.observe" else spec
                        for spec in getattr(plan, field)
                    )
                    for field in ("tool_specs", "deferred_tool_specs")
                }
            )
        elif mode == "deferred":
            context.context_plan = plan.model_copy(
                update={
                    "tool_names": (),
                    "tool_specs": (),
                    "deferred_tool_names": tuple(spec.name for spec in plan.pinned_tool_specs),
                    "deferred_tool_specs": plan.pinned_tool_specs,
                }
            )
        return await original(self, context)

    monkeypatch.setattr(BrowserWorkflowRunner, "__call__", pinned)

    class Provider(FakeBrowserProvider):
        def _observation(self, url: str) -> BrowserObservation:
            return (
                super()
                ._observation(url)
                .model_copy(update={"text": "Report ready" if self.actions else "Pending"})
            )

    provider = Provider()
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported available progress.")]),
        enabled_tools=["browser.navigate", "browser.observe"]
        + (["browser.act"] if mode != "ambiguous" else []),
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        assert len(approvals) == int(mode == "deferred")
        assert not provider.actions
        if approvals:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        assert ("browser_workflow" in saved.working_state) == (mode == "deferred")
    assert len(provider.navigations) == len(provider.actions) == int(mode == "deferred")
