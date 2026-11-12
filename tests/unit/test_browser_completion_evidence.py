"""Positive completion evidence must never cause an action to be repeated."""

from typing import Any

import pytest

from agent_core.adapters.browser.extraction import extracted_observation
from agent_core.domain.browser import (
    BROWSER_AUTH_INTERRUPTION_MARKER,
    BrowserCondition,
    BrowserObservation,
    BrowserProviderError,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
)
from agent_core.tools.browser_act import BrowserActTool
from agent_core.tools.browser_conditions import check_browser_condition
from agent_core.tools.browser_observe import BrowserObserveTool
from tests.contract.support import tool_context
from tests.unit.test_browser_tools import FakeBrowserProvider


@pytest.mark.parametrize("signal", ["needs_user", "authentication_required", "observation"])
@pytest.mark.parametrize("kind", ["control", "text", "row"])
async def test_authentication_during_postcondition_preserves_uncertain_sign_in(
    signal: str, kind: str
) -> None:
    class Provider(FakeBrowserProvider):
        async def observe(self) -> BrowserObservation:
            self.observation_count += 1
            if signal != "observation":
                raise BrowserProviderError(f"tool.browser.{signal}", retryable=False)
            return self._observation("https://example.org/account").model_copy(
                update={"interruption": "needs_user"}
            )

        async def extract(self, request: Any) -> BrowserObservation:
            observed = await self.observe()
            return observed.model_copy(
                update={
                    "revision": "extracted",
                    "extraction": extracted_observation(
                        {
                            "status": "extracted",
                            "rows": [],
                            "source_nodes": 1,
                            "source_scan_limit_reached": False,
                            "row_nodes": 0,
                            "row_scan_limit_reached": False,
                        },
                        request,
                        "extracted",
                    ),
                }
            )

    provider = Provider()
    condition: dict[str, Any] = {"role": "button", "name": "Saved", "timeout_ms": 1000}
    if kind == "text":
        condition = {"evidence": {"kind": "text", "text": "Saved"}, "timeout_ms": 1000}
    elif kind == "row":
        condition = {
            "evidence": {
                "kind": "row",
                "collection_kind": "table",
                "fields": [{"name": "receipt", "column": 0, "type": "integer", "value": 42}],
            },
            "timeout_ms": 1000,
        }
    result = await BrowserActTool(provider).execute(
        {
            "kind": "click",
            "ref": "element-1",
            "expected_revision": "revision-1",
            "postcondition": condition,
        },
        tool_context(),
    )
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == "tool.browser.outcome_unknown"
    assert result.failure.external_text == BROWSER_AUTH_INTERRUPTION_MARKER
    assert not result.failure.retryable
    assert provider.observation_count == 1 and len(provider.actions) == 1


async def test_initial_interruption_cannot_satisfy_a_control_or_start_polling() -> None:
    provider = FakeBrowserProvider()
    observation = provider._observation("https://example.org/account").model_copy(
        update={"interruption": "needs_user"}
    )
    with pytest.raises(BrowserProviderError, match=r"tool\.browser\.needs_user"):
        await check_browser_condition(
            provider, observation, BrowserCondition(role="link", name="Open activity")
        )
    assert provider.observation_count == 0 and not provider.actions


@pytest.mark.parametrize("reason", ["provider_unavailable", "output_invalid", "page_changed"])
async def test_other_postcondition_failures_do_not_request_sign_in(reason: str) -> None:
    class Provider(FakeBrowserProvider):
        async def observe(self) -> BrowserObservation:
            raise BrowserProviderError(f"tool.browser.{reason}", retryable=False)

    provider = Provider()
    result = await BrowserActTool(provider).execute(
        {
            "kind": "click",
            "ref": "element-1",
            "expected_revision": "revision-1",
            "postcondition": {"role": "button", "name": "Saved", "timeout_ms": 1000},
        },
        tool_context(),
    )
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == "tool.browser.outcome_unknown"
    assert result.failure.external_text != BROWSER_AUTH_INTERRUPTION_MARKER
    assert not result.failure.retryable and len(provider.actions) == 1


class EvidenceProvider(FakeBrowserProvider):
    text = "Account overview\nSaved successfully"
    region_text = "Saved successfully"
    truncated = False

    def _observation(self, url: str) -> BrowserObservation:
        return (
            super()
            ._observation(url)
            .model_copy(
                update={
                    "text": self.text,
                    "regions": (
                        BrowserSemanticRegion(
                            ref="status",
                            kind="status",
                            text=self.region_text,
                            text_truncated=self.truncated,
                        ),
                    ),
                    "region_coverage": BrowserRegionCoverage(scanned_nodes=3),
                }
            )
        )


@pytest.mark.parametrize(
    "evidence",
    [
        {"kind": "text", "text": "Saved successfully"},
        {"kind": "region", "region_kind": "status", "text": "Saved successfully"},
        {"kind": "location", "url": "https://example.org/account"},
    ],
)
async def test_positive_completion_evidence_is_a_read(evidence: dict[str, Any]) -> None:
    provider = EvidenceProvider()
    result = await BrowserObserveTool(provider).execute(
        {"wait_for": {"evidence": evidence, "timeout_ms": 0}}, tool_context()
    )
    assert result.ok, "positive page evidence is not yet supported"
    assert result.structured is not None
    assert result.structured["condition"]["status"] == "satisfied"
    assert provider.observation_count == 1 and not provider.actions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Save failed", "failed"),
        ("Saved successfully\nSave failed", "ambiguous"),
        ("Not Saved successfully", "not_observed"),
    ],
)
async def test_negative_or_conflicting_evidence_never_replays_a_write(
    text: str, expected: str
) -> None:
    provider = EvidenceProvider()
    provider.text = text
    result = await BrowserActTool(provider).execute(
        {
            "kind": "click",
            "expected_revision": "revision-1",
            "ref": "element-1",
            "postcondition": {
                "evidence": {"kind": "text", "text": "Saved successfully"},
                "failure_evidence": {"kind": "text", "text": "Save failed"},
                "timeout_ms": 0,
            },
        },
        tool_context(),
    )
    assert result.ok and result.structured is not None
    assert result.structured["condition"]["status"] == expected
    assert len(provider.actions) == 1


async def test_truncated_region_cannot_prove_completion() -> None:
    provider = EvidenceProvider()
    provider.truncated = True
    result = await BrowserObserveTool(provider).execute(
        {
            "wait_for": {
                "evidence": {
                    "kind": "region",
                    "region_kind": "status",
                    "text": "Saved successfully",
                },
                "timeout_ms": 0,
            }
        },
        tool_context(),
    )
    assert result.ok and result.structured is not None
    assert result.structured["condition"]["status"] == "not_observed"


async def test_one_stale_verification_read_is_reconciled_without_repeating_the_action() -> None:
    from agent_core.domain.browser import BrowserProviderError

    class Provider(EvidenceProvider):
        captures = 0
        text = "Pending"

        async def observe(self) -> BrowserObservation:
            self.captures += 1
            if self.captures == 1:
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            self.text = "Saved successfully"
            return await super().observe()

    provider = Provider()
    result = await BrowserActTool(provider).execute(
        {
            "kind": "click",
            "expected_revision": "revision-1",
            "ref": "element-1",
            "postcondition": {
                "evidence": {"kind": "text", "text": "Saved successfully"},
                "timeout_ms": 1000,
            },
        },
        tool_context(),
    )
    assert result.ok and result.structured is not None
    assert result.structured["condition"]["status"] == "satisfied"
    assert len(provider.actions) == 1 and provider.captures == 2


@pytest.mark.parametrize(
    "evidence",
    [
        {"kind": "text", "text": ""},
        {"kind": "text", "text": "Saved", "selector": "body"},
        {"kind": "location", "url": "http://localhost/"},
        {"kind": "region", "region_kind": "script", "text": "Saved"},
    ],
)
async def test_invalid_evidence_refuses_before_binding(evidence: dict[str, Any]) -> None:
    provider = EvidenceProvider()
    result = await BrowserObserveTool(provider).execute(
        {"wait_for": {"evidence": evidence}}, tool_context()
    )
    assert not result.ok and not provider.execution_contexts


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (["42"], "satisfied"),
        (["43"], "not_observed"),
        (["42", "42"], "ambiguous"),
        (["42 pending"], "not_observed"),
    ],
)
async def test_typed_row_reconciliation_reads_fresh_evidence_only(
    rows: list[str], expected: str
) -> None:
    class Provider(EvidenceProvider):
        reads = 0

        async def extract(self, request: Any) -> BrowserObservation:
            assert request.expected_revision == "revision-1"
            self.reads += 1
            raw = {
                "status": "extracted",
                "source_name": "Receipts",
                "rows": [[{"text": value, "text_truncated": False}] for value in rows],
                "source_nodes": 2,
                "source_scan_limit_reached": False,
                "row_nodes": len(rows),
                "row_scan_limit_reached": False,
                "omitted_rows": 0,
            }
            return self._observation("https://example.org/account").model_copy(
                update={
                    "revision": "extracted",
                    "extraction": extracted_observation(raw, request, "extracted"),
                }
            )

    provider = Provider()
    result = await BrowserObserveTool(provider).execute(
        {
            "wait_for": {
                "evidence": {
                    "kind": "row",
                    "collection_kind": "table",
                    "fields": [{"name": "receipt", "column": 0, "type": "integer", "value": 42}],
                },
                "timeout_ms": 100,
            }
        },
        tool_context(),
    )
    assert result.ok and result.structured is not None
    assert result.structured["condition"]["status"] == expected
    assert result.structured["condition"]["observations"] == 2
    assert provider.reads == 1 and not provider.actions


def test_document_row_predicate_rejects_a_different_extraction_scope() -> None:
    from agent_core.domain.browser_evidence import BrowserRowEvidence, evidence_status

    evidence = BrowserRowEvidence.model_validate(
        {
            "kind": "row",
            "collection_kind": "table",
            "fields": [{"name": "receipt", "column": 0, "type": "integer", "value": 42}],
        }
    )
    request = evidence.extraction_request("revision-1").model_copy(
        update={"scope": "main_document_and_open_shadow"}
    )
    extracted = extracted_observation(
        {
            "status": "extracted",
            "source_name": "Receipts",
            "rows": [[{"text": "42", "text_truncated": False}]],
            "source_nodes": 2,
            "source_scan_limit_reached": False,
            "row_nodes": 1,
            "row_scan_limit_reached": False,
            "omitted_rows": 0,
        },
        request,
        "revision-1",
    )
    observed = (
        EvidenceProvider()
        ._observation("https://example.org/account")
        .model_copy(update={"extraction": extracted})
    )
    assert evidence_status(observed, evidence) == "not_observed"
