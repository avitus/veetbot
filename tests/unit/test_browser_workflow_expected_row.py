"""A reviewed extraction advances only with its expected typed row in the current result."""

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

EXPECTED = [{"name": "item", "column": 0, "type": "string", "value": "Ready"}]


def test_extraction_accepts_expected_typed_row() -> None:
    step = BrowserWorkflowStep.model_validate({**EXTRACT, "expected_row": EXPECTED})
    assert step.expected_row is not None and step.expected_row[0].value == "Ready"


@pytest.mark.parametrize(
    "expected",
    [
        [],
        EXPECTED * 9,
        [{**EXPECTED[0], "name": "unknown"}],
        [{**EXPECTED[0], "column": 1}],
        [{**EXPECTED[0], "type": "integer", "value": 1}],
        [{**EXPECTED[0], "value": True}],
        [{**EXPECTED[0], "required": False}],
        [{**EXPECTED[0], "value": ""}],
        [{**EXPECTED[0], "value": "x" * 257}],
        EXPECTED * 2,
    ],
)
def test_expected_row_rejects_invalid_or_different_fields(expected: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        BrowserWorkflowStep.model_validate({**EXTRACT, "expected_row": expected})


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "navigate", "url": "https://example.org/account"},
        {"kind": "observe"},
        CATALOG["recipes"][0]["steps"][1],
        {"kind": "collect_feed", "role": "button", "name": "Account"},
    ],
)
def test_expected_row_is_extraction_only(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BrowserWorkflowStep.model_validate({**step, "expected_row": EXPECTED})


def test_expected_row_preserves_legacy_identity_and_binds_values() -> None:
    from agent_core.runtime.browser_workflows import _definition

    document = deepcopy(CATALOG["recipes"][0])
    document["steps"] = [document["steps"][0], EXTRACT.copy()]
    legacy = _definition(BrowserWorkflowRecipe.model_validate(document))
    assert legacy == "846f4b5ddbeb137a4bf4b5abd2664ef5ebf4f89c3474d800dec24d34596cdd11"
    document["steps"][1]["expected_row"] = None
    assert _definition(BrowserWorkflowRecipe.model_validate(document)) == legacy
    document["steps"][1]["expected_row"] = deepcopy(EXPECTED)
    bound = _definition(BrowserWorkflowRecipe.model_validate(document))
    assert bound != legacy
    document["steps"][1]["expected_row"][0]["value"] = "Other"
    assert _definition(BrowserWorkflowRecipe.model_validate(document)) != bound


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("unique", "verified"),
        ("integer", "verified"),
        ("number", "verified"),
        ("boolean", "verified"),
        ("multiple_fields", "verified"),
        ("split_rows", "insufficient_evidence"),
        ("missing", "insufficient_evidence"),
        ("empty", "insufficient_evidence"),
        ("duplicate", "insufficient_evidence"),
        ("invalid", "unverified"),
        ("partial_allowed", "verified"),
        ("partial_required", "insufficient_evidence"),
        ("stale_revision", "unverified"),
        ("forged_condition", "insufficient_evidence"),
    ],
)
async def test_only_the_expected_row_can_reach_a_later_action(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    catalog = deepcopy(CATALOG)
    steps = catalog["recipes"][0]["steps"]
    field_type = mode if mode in {"integer", "number", "boolean"} else "string"
    expected_value = {"integer": 2, "number": 2.5, "boolean": True}.get(mode, "Ready")
    steps.insert(
        1,
        {
            **EXTRACT,
            "fields": [{"name": "item", "column": 0, "type": field_type}],
            "expected_row": [{**EXPECTED[0], "type": field_type, "value": expected_value}],
            "require_complete": mode == "partial_required",
        },
    )
    if mode in {"multiple_fields", "split_rows"}:
        field = {"name": "count", "column": 1, "type": "integer"}
        steps[1]["fields"].append(field)
        steps[1]["expected_row"].append({**field, "value": 2})
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
            values = (
                []
                if mode == "empty"
                else ["Ready", "Ready"]
                if mode == "duplicate"
                else ["Other", "Ready"]
            )
            if mode in {"missing", "forged_condition"}:
                values = ["Other", "Pending"]
            if mode == "split_rows":
                values = ["Ready", "Other"]
            if mode in {"integer", "number", "boolean"}:
                values = {
                    "integer": ["1", "2"],
                    "number": ["1", "2.5"],
                    "boolean": ["false", "true"],
                }[mode]
            count = len(values)
            raw: dict[str, Any] = {
                "status": "extracted",
                "source_name": "Fixture",
                "rows": [
                    [{"text": value, "text_truncated": mode == "invalid"}]
                    + (
                        [{"text": str(index + 1), "text_truncated": False}]
                        if mode in {"multiple_fields", "split_rows"}
                        else []
                    )
                    for index, value in enumerate(values)
                ],
                "source_nodes": 2,
                "source_scan_limit_reached": False,
                "row_nodes": count,
                "row_scan_limit_reached": False,
                "omitted_rows": int(mode in {"partial_required", "partial_allowed"}),
            }
            extracted = extracted_observation(raw, request, "extracted-revision")
            result = self._observation("https://example.org/account").model_copy(
                update={"revision": "extracted-revision", "extraction": extracted}
            )
            if mode == "stale_revision":
                result = result.model_copy(
                    update={"extraction": extracted.model_copy(update={"revision": "old"})}
                )
            if mode == "forged_condition":
                from agent_core.domain.browser import BrowserConditionResult

                result = result.model_copy(
                    update={
                        "condition": BrowserConditionResult(
                            status="satisfied", observations=1, elapsed_ms=0
                        )
                    }
                )
            return result

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
