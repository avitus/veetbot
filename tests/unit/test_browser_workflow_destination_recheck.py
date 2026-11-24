"""Later target windows need positive destination evidence in their own capture."""

from typing import Any

import pytest
from pydantic import ValidationError

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import BrowserConditionResult, BrowserObservation
from agent_core.domain.browser_workflows import BrowserWorkflowRecipe, BrowserWorkflowStep
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_workflow_navigation import navigation_catalog
from tests.unit.test_browser_workflow_target_discovery import WindowProvider
from tests.unit.test_config import base_environment


def recheck_catalog() -> dict[str, Any]:
    catalog = navigation_catalog()
    catalog["recipes"][0]["steps"][1]["recheck_destination"] = True
    return catalog


def test_reviewed_action_accepts_explicit_destination_recheck() -> None:
    recipe = BrowserWorkflowRecipe.model_validate(recheck_catalog()["recipes"][0])
    assert recipe.steps[1].recheck_destination


@pytest.mark.parametrize("value", [1, "true", None])
def test_recheck_is_a_strict_boolean(value: object) -> None:
    step = recheck_catalog()["recipes"][0]["steps"][1]
    with pytest.raises(ValidationError):
        BrowserWorkflowStep.model_validate({**step, "recheck_destination": value})


def test_recheck_is_bound_to_a_conditioned_navigation_and_action() -> None:
    catalog = recheck_catalog()["recipes"][0]
    with pytest.raises(ValidationError):
        BrowserWorkflowStep.model_validate({**catalog["steps"][0], "recheck_destination": True})
    del catalog["steps"][0]["postcondition"]
    with pytest.raises(ValidationError, match="conditioned navigation"):
        BrowserWorkflowRecipe.model_validate(catalog)


def test_recheck_preserves_legacy_identity_and_binds_opt_in() -> None:
    from agent_core.runtime.browser_workflows import _definition

    document = navigation_catalog()["recipes"][0]
    legacy = _definition(BrowserWorkflowRecipe.model_validate(document))
    # Checkpoints created before ADR-0350 carry this digest for the default recipe.
    assert legacy == "3c1b0ccca2c15a50300cf6e135e989df877223a3eb20ca0cc597f11c90e636aa"
    document["steps"][1]["recheck_destination"] = False
    assert _definition(BrowserWorkflowRecipe.model_validate(document)) == legacy
    document["steps"][1]["recheck_destination"] = True
    assert _definition(BrowserWorkflowRecipe.model_validate(document)) != legacy


@pytest.mark.parametrize(
    ("mode", "expected", "windows"),
    [
        ("ready", "verified", 2),
        ("location", "verified", 2),
        ("truncated_url", "unverified", 2),
        ("truncated_url_fourbytes", "unverified", 2),
        ("region", "verified", 2),
        ("control", "verified", 2),
        ("duplicate_control", "unverified", 2),
        ("disabled_control", "unverified", 2),
        ("truncated_text", "unverified", 2),
        ("missing_coverage", "unverified", 2),
        ("missing_failure_coverage", "unverified", 2),
        ("truncated_region", "unverified", 2),
        ("foreign", "unverified", 2),
        ("missing", "unverified", 2),
        ("failure", "observed_failure", 2),
        ("conflict", "unverified", 2),
        ("forged_condition", "unverified", 2),
        ("fourth", "verified", 4),
        ("exhausted", "target_not_observed", 4),
    ],
)
async def test_each_control_window_reproves_the_destination_before_approval(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str, windows: int
) -> None:
    from agent_core.domain.browser import (
        BrowserElement,
        BrowserRegionCoverage,
        BrowserSemanticRegion,
        BrowserTextCoverage,
    )

    catalog = recheck_catalog()
    condition = catalog["recipes"][0]["steps"][0]["postcondition"]
    clipped_url = mode.startswith("truncated_url")
    prefix_length = 4093 if mode == "truncated_url_fourbytes" else 4095
    url_prefix = "https://example.org/" + "x" * (prefix_length - len("https://example.org/"))
    if clipped_url or mode in {"location", "missing_failure_coverage"}:
        condition["evidence"] = {
            "kind": "location",
            "url": url_prefix if clipped_url else "https://example.org/account",
        }
    elif mode in {"region", "truncated_region"}:
        condition["evidence"] = {"kind": "region", "region_kind": "status", "text": "Account ready"}
    elif mode in {"control", "duplicate_control", "disabled_control"}:
        del condition["evidence"]
        condition.update(role="button", name="Ready", disabled=False)
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            catalog
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )

    class Provider(WindowProvider):
        def _observation(self, url: str) -> BrowserObservation:
            text = "Report ready" if self.actions else "Account ready"
            if self.window and not self.actions:
                text = {
                    "missing": "Pending",
                    "failure": "Account unavailable",
                    "conflict": "Account ready\nAccount unavailable",
                    "forged_condition": "Pending",
                }.get(mode, text)
            result = (
                super()
                ._observation(url)
                .model_copy(
                    update={
                        "text": text,
                        "text_coverage": BrowserTextCoverage(
                            scanned_nodes=1, scanned_text_characters=len(text)
                        ),
                    }
                )
            )
            if clipped_url:
                suffix = "😀" if mode == "truncated_url_fourbytes" else "é"
                result = result.model_copy(
                    update={"url": url_prefix + (suffix if self.window else "")}
                )
            if self.window and mode in {"missing_coverage", "missing_failure_coverage"}:
                result = result.model_copy(update={"text_coverage": None})
            if mode in {"control", "duplicate_control", "disabled_control"}:
                marker = BrowserElement(
                    ref="ready-marker",
                    role="button",
                    name="Ready",
                    disabled=bool(self.window and mode == "disabled_control"),
                )
                markers = (
                    (marker, marker.model_copy(update={"ref": "duplicate-marker"}))
                    if self.window and mode == "duplicate_control"
                    else (marker,)
                )
                result = result.model_copy(update={"elements": (*result.elements, *markers)})
            if mode in {"region", "truncated_region"}:
                result = result.model_copy(
                    update={
                        "regions": (
                            BrowserSemanticRegion(
                                ref="ready-region",
                                kind="status",
                                text="Account ready",
                                text_truncated=bool(self.window and mode == "truncated_region"),
                            ),
                        ),
                        "region_coverage": BrowserRegionCoverage(scanned_nodes=1),
                    }
                )
            if self.window and mode == "truncated_text":
                result = result.model_copy(
                    update={
                        "text_coverage": BrowserTextCoverage(
                            scanned_nodes=1,
                            scanned_text_characters=len(text),
                            omitted_text_bytes=20,
                        )
                    }
                )
            if self.window and mode == "foreign":
                result = result.model_copy(update={"url": "https://elsewhere.org/account"})
            if mode == "forged_condition" and self.window:
                result = result.model_copy(
                    update={
                        "condition": BrowserConditionResult(
                            status="satisfied",
                            observations=1,
                            elapsed_ms=0,
                        )
                    }
                )
            return result

    provider = Provider(target_window=3 if mode == "fourth" else 5 if mode == "exhausted" else 1)
    accepted = expected == "verified"
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported current evidence.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        assert len(approvals) == int(accepted), "only current-window proof may reach approval"
        assert not provider.actions
        if approvals:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == expected
        assert state["completed_steps"] == (2 if accepted else 1)
        assert state["target_search"] == {"step": 1, "windows": windows}
        assert saved.working_state["browser_workflow_report_only"]
    assert len(provider.expansions) == windows - 1
    assert len(provider.actions) == int(accepted)
    if accepted:
        assert provider.actions[0].expected_revision == f"window-{windows - 1}"


def test_recheck_rejects_rows_that_need_a_separate_capture() -> None:
    recipe = recheck_catalog()["recipes"][0]
    recipe["steps"][0]["postcondition"]["evidence"] = {
        "kind": "row",
        "collection_kind": "table",
        "fields": [{"name": "status", "column": 0, "type": "string", "value": "Ready"}],
    }
    with pytest.raises(ValidationError, match="capture-local evidence"):
        BrowserWorkflowRecipe.model_validate(recipe)
