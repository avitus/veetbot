"""Reviewed region evidence narrows target reads without granting authority."""

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserElement,
    BrowserObservation,
    BrowserObservationCoverage,
    BrowserObservationExpansion,
    BrowserObservationFocus,
    BrowserProviderError,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
)
from agent_core.domain.browser_workflows import BrowserWorkflowCatalog, BrowserWorkflowStep
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment

REGION = {"kind": "region", "region_kind": "form", "text": "Account activity"}


def region_catalog() -> dict[str, Any]:
    catalog = deepcopy(CATALOG)
    catalog["recipes"][0]["steps"][1]["target_region"] = REGION.copy()
    return catalog


def test_reviewed_actions_accept_closed_region_evidence() -> None:
    recipe = BrowserWorkflowCatalog.model_validate(region_catalog()).recipes[0]
    assert recipe.steps[1].model_dump()["target_region"] == REGION


def test_region_condition_changes_definition_identity() -> None:
    from agent_core.runtime.browser_workflows import _definition

    catalog = region_catalog()
    first = _definition(BrowserWorkflowCatalog.model_validate(catalog).recipes[0])
    catalog["recipes"][0]["steps"][1]["target_region"]["text"] = "Other account"
    assert _definition(BrowserWorkflowCatalog.model_validate(catalog).recipes[0]) != first


@pytest.mark.parametrize(
    "region",
    [
        {**REGION, "selector": "#account"},
        {**REGION, "text": " "},
        {**REGION, "region_kind": "iframe"},
        {"kind": "text", "text": "Account activity"},
    ],
)
def test_region_targets_reject_unreviewed_modes(region: dict[str, str]) -> None:
    step = deepcopy(CATALOG["recipes"][0]["steps"][1])
    with pytest.raises(ValidationError):
        BrowserWorkflowStep.model_validate({**step, "target_region": region})


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
        {"kind": "collect_feed", "role": "main", "name": "Home timeline"},
    ],
)
def test_only_actions_admit_region_targets(step: dict[str, Any]) -> None:
    BrowserWorkflowStep.model_validate(step)
    with pytest.raises(ValidationError, match="workflow step mixes modes"):
        BrowserWorkflowStep.model_validate({**step, "target_region": REGION})


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("normal", "verified"),
        ("whitespace", "verified"),
        ("missing", "region_not_observed"),
        ("duplicate", "ambiguous_region"),
        ("truncated", "region_not_observed"),
        ("no_focus", "unverified"),
        ("paged", "verified"),
        ("exhausted", "target_not_observed"),
        ("lost_focus", "unverified"),
        ("stale", "unverified"),
        ("foreign", "unverified"),
    ],
)
async def test_region_target_requires_unique_complete_evidence_and_focused_result(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    catalog = region_catalog()
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
            super().__init__()
            self.expansions: list[BrowserObservationExpansion] = []

        def _observation(self, url: str) -> BrowserObservation:
            observed = super()._observation(url)
            focused = bool(self.expansions)
            paged = mode in {"paged", "exhausted", "lost_focus"}
            region = BrowserSemanticRegion(
                ref="current-region",
                kind="form",
                text=" Account\nactivity " if mode == "whitespace" else "Account activity",
                text_truncated=mode == "truncated",
            )
            return observed.model_copy(
                update={
                    "url": "https://other.example/account"
                    if focused and mode == "foreign"
                    else url,
                    "region_coverage": BrowserRegionCoverage(scanned_nodes=10),
                    "revision": "focused-revision" if focused else "current-revision",
                    "text": "Report ready" if self.actions else "Account overview",
                    "elements": (
                        BrowserElement(
                            ref="focused-target",
                            role="link",
                            name="Unrelated"
                            if paged and (len(self.expansions) < 3 or mode == "exhausted")
                            else "Open activity",
                        ),
                    )
                    if focused
                    else (
                        *observed.elements,
                        observed.elements[0].model_copy(update={"ref": "other"}),
                    ),
                    "regions": ()
                    if mode == "missing"
                    else (region, region.model_copy(update={"ref": "duplicate"}))
                    if mode == "duplicate"
                    else (region,),
                    "focus": BrowserObservationFocus(
                        region_ref="rotated-focus",
                        text_offset=0,
                        text_total_bytes=len(
                            "Report ready" if self.actions else "Account overview"
                        ),
                        text_cursor="a" * 32,
                    )
                    if focused
                    and mode != "no_focus"
                    and not (mode == "lost_focus" and len(self.expansions) > 1)
                    else None,
                    "coverage": BrowserObservationCoverage(
                        candidate_offset=len(self.expansions) * 256,
                        scanned_candidates=1,
                        next_cursor=f"{len(self.expansions) + 1:032x}",
                    )
                    if paged and focused
                    else None,
                }
            )

        async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
            if not self.expansions:
                assert request.region_ref == "current-region"
                assert request.expected_revision == "current-revision"
            else:
                assert request.cursor == f"{len(self.expansions) + 1:032x}"
            self.expansions.append(request)
            if mode == "stale":
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            return self._observation("https://example.org/account")

    provider = Provider()
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
        assert len(approvals) == int(expected == "verified")
        assert not provider.actions
        if approvals:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == expected
        assert state["target_search"]["windows"] == 1 + len(provider.expansions)
    assert len(provider.actions) == int(expected == "verified")
    if provider.actions:
        assert provider.actions[0].ref == "focused-target"
        assert provider.actions[0].expected_revision == "focused-revision"
