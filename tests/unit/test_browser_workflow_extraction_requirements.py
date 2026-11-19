"""Reviewed extraction requirements cannot turn partial evidence into completion."""

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

import agent_core.bootstrap as bootstrap
from agent_core.adapters.browser.extraction import extracted_observation
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import BrowserObservation
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.domain.browser_workflows import BrowserWorkflowRecipe, BrowserWorkflowStep
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflow_extraction import EXTRACT
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


def test_extraction_accepts_explicit_bounded_evidence_requirements() -> None:
    step = BrowserWorkflowStep.model_validate(
        {**deepcopy(EXTRACT), "minimum_rows": 2, "require_complete": True}
    )
    assert step.minimum_rows == 2
    assert step.require_complete


@pytest.mark.parametrize(
    "requirements",
    [
        {"minimum_rows": -1},
        {"minimum_rows": 51},
        {"minimum_rows": True},
        {"minimum_rows": 2.0},
        {"minimum_rows": "2"},
        {"minimum_rows": 3, "row_limit": 2},
        {"require_complete": 1},
        {"require_complete": "true"},
    ],
)
def test_extraction_rejects_invalid_or_impossible_requirements(
    requirements: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        BrowserWorkflowStep.model_validate({**EXTRACT, **requirements})


@pytest.mark.parametrize("field", ["minimum_rows", "require_complete"])
@pytest.mark.parametrize(
    "step",
    [
        {"kind": "navigate", "url": "https://example.org/account"},
        {"kind": "observe"},
        CATALOG["recipes"][0]["steps"][1],
        {"kind": "collect_feed", "role": "button", "name": "Account"},
    ],
)
def test_extraction_requirements_cannot_be_attached_to_other_steps(
    field: str, step: dict[str, Any]
) -> None:
    BrowserWorkflowStep.model_validate(step)
    with pytest.raises(ValidationError, match="workflow step mixes modes"):
        BrowserWorkflowStep.model_validate({**step, field: 1 if field == "minimum_rows" else True})


def test_requirements_preserve_legacy_identity_and_bind_nondefault_values() -> None:
    from agent_core.runtime.browser_workflows import _definition

    document = deepcopy(CATALOG["recipes"][0])
    document["steps"] = [document["steps"][0], EXTRACT.copy()]
    legacy = _definition(BrowserWorkflowRecipe.model_validate(document))
    assert legacy == "846f4b5ddbeb137a4bf4b5abd2664ef5ebf4f89c3474d800dec24d34596cdd11"
    document["steps"][1].update(minimum_rows=0, require_complete=False)
    assert _definition(BrowserWorkflowRecipe.model_validate(document)) == legacy
    for requirement in ({"minimum_rows": 1}, {"require_complete": True}):
        changed = deepcopy(document)
        changed["steps"][1].update(requirement)
        assert _definition(BrowserWorkflowRecipe.model_validate(changed)) != legacy


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("complete", "verified"),
        ("empty", "insufficient_evidence"),
        ("empty_allowed", "verified"),
        ("short", "insufficient_evidence"),
        ("omitted", "insufficient_evidence"),
        ("source_limit", "insufficient_evidence"),
        ("row_limit", "insufficient_evidence"),
        ("byte_limit", "insufficient_evidence"),
        ("partial_allowed", "verified"),
        ("invalid", "unverified"),
    ],
)
async def test_only_sufficient_extraction_can_reach_a_later_action(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    catalog = deepcopy(CATALOG)
    steps = catalog["recipes"][0]["steps"]
    steps.insert(
        1,
        {
            **EXTRACT,
            "minimum_rows": 0 if mode == "empty_allowed" else 2,
            "require_complete": mode != "partial_allowed",
        },
    )
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
            self.requests: list[BrowserExtractionRequest] = []

        def _observation(self, url: str) -> BrowserObservation:
            return (
                super()
                ._observation(url)
                .model_copy(update={"text": "Report ready" if self.actions else "Pending"})
            )

        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            self.requests.append(request)
            count = 0 if mode.startswith("empty") else 1 if mode == "short" else 2
            raw: dict[str, Any] = {
                "status": "extracted",
                "source_name": "Fixture",
                "rows": [
                    [{"text": f"Item {i}", "text_truncated": mode == "invalid"}]
                    for i in range(count)
                ],
                "source_nodes": 2,
                "source_scan_limit_reached": mode == "source_limit",
                "row_nodes": count,
                "row_scan_limit_reached": mode == "row_limit",
                "omitted_rows": int(mode in {"omitted", "partial_allowed"}),
            }
            extracted = extracted_observation(raw, request, "extracted-revision").model_copy(
                update={"byte_limit_reached": mode == "byte_limit"}
            )
            return self._observation("https://example.org/account").model_copy(
                update={"revision": "extracted-revision", "extraction": extracted}
            )

    provider = Provider()
    accepted = expected == "verified"
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Reported available evidence.")]),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        assert len(approvals) == int(accepted), "insufficient evidence reached a later approval"
        assert not provider.actions
        if approvals:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        state = saved.working_state["browser_workflow"]
        assert state["status"] == expected
        assert state["completed_steps"] == (3 if accepted else 1)
        assert saved.working_state["browser_workflow_report_only"]
    assert len(provider.requests) == 1
    assert len(provider.actions) == int(accepted)
