"""Versioned row completion preserves scope, finite reads and uncertain writes."""

import asyncio
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from agent_core.adapters.browser.extraction import extracted_observation
from agent_core.domain.browser import (
    BROWSER_AUTH_INTERRUPTION_MARKER,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.tools.browser_act import BrowserActTool, PinnedBrowserActTool, ShadowBrowserActTool
from agent_core.tools.browser_observe import (
    ArticleBrowserObserveTool,
    BrowserObserveTool,
    CompletionBrowserObserveTool,
    ShadowBrowserObserveTool,
)
from tests.contract.support import tool_context
from tests.unit.test_browser_tools import FakeBrowserProvider

ROW = {
    "kind": "row",
    "collection_kind": "table",
    "fields": [{"name": "receipt", "column": 0, "type": "integer", "value": 42}],
}
TOOLS = [
    BrowserObserveTool,
    ArticleBrowserObserveTool,
    ShadowBrowserObserveTool,
    CompletionBrowserObserveTool,
    PinnedBrowserActTool,
    BrowserActTool,
    ShadowBrowserActTool,
]


def arguments(action: bool, **condition: Any) -> dict[str, Any]:
    check = {"evidence": ROW, "timeout_ms": 100, **condition}
    return (
        {
            "kind": "click",
            "expected_revision": "revision-1",
            "ref": "element-1",
            "postcondition": check,
        }
        if action
        else {"wait_for": check}
    )


class RowProvider(FakeBrowserProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[BrowserExtractionRequest] = []
        self.rows = ["42"]
        self.truncated = False
        self.text = ""
        self.stale_once = False
        self.wrong_scope = False
        self.reason: str | None = None

    async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
        self.requests.append(request)
        if self.reason:
            raise BrowserProviderError(self.reason, retryable=False)
        if self.stale_once and len(self.requests) == 1:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        if self.wrong_scope:
            request = request.model_copy(update={"scope": "main_document"})
        return self.page(request)

    def page(self, request: BrowserExtractionRequest) -> BrowserObservation:
        raw = {
            "status": "extracted",
            "source_name": "Receipts",
            "rows": [[{"text": value, "text_truncated": self.truncated}] for value in self.rows],
            "source_nodes": 2,
            "source_scan_limit_reached": False,
            "row_nodes": len(self.rows),
            "row_scan_limit_reached": False,
            "omitted_rows": 0,
        }
        return self._observation("https://example.org/account").model_copy(
            update={
                "revision": "extracted",
                "text": self.text,
                "extraction": extracted_observation(raw, request, "extracted"),
            }
        )


@pytest.mark.parametrize("tool", TOOLS)
async def test_pinned_version_selects_row_scope_and_valid_output(tool: Any) -> None:
    provider = RowProvider()
    action = issubclass(tool, BrowserActTool)
    shadow = tool in {CompletionBrowserObserveTool, ShadowBrowserActTool}
    result = await tool(provider).execute(arguments(action), tool_context())
    assert result.ok and result.structured is not None
    Draft202012Validator(tool.spec.output_schema).validate(result.structured)
    assert result.structured["condition"]["status"] == "satisfied"
    assert result.structured["extraction"]["version"] == (2 if shadow else 1)
    assert [r.scope for r in provider.requests] == [
        "main_document_and_open_shadow" if shadow else "main_document"
    ]
    assert len(provider.actions) == int(action)


@pytest.mark.parametrize("action", [False, True])
@pytest.mark.parametrize("extra", ["scope", "selector", "profile_id"])
async def test_model_scope_or_authority_override_refuses_before_binding(
    action: bool,
    extra: str,
) -> None:
    provider = RowProvider()
    tool = ShadowBrowserActTool if action else CompletionBrowserObserveTool
    result = await tool(provider).execute(
        arguments(action, evidence={**ROW, extra: "main_document"}), tool_context()
    )
    assert not result.ok and not provider.execution_contexts
    assert not provider.actions and not provider.requests


@pytest.mark.parametrize("action", [False, True])
@pytest.mark.parametrize("failure", ["wrong_scope", "unsupported", "authentication_required"])
async def test_refused_verification_never_falls_back_or_replays(action: bool, failure: str) -> None:
    provider = RowProvider()
    provider.wrong_scope = failure == "wrong_scope"
    if failure != "wrong_scope":
        provider.reason = f"tool.browser.{failure}"
    tool = ShadowBrowserActTool if action else CompletionBrowserObserveTool
    result = await tool(provider).execute(arguments(action), tool_context())
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == (
        "tool.browser.outcome_unknown"
        if action
        else "tool.browser.output_invalid"
        if failure == "wrong_scope"
        else provider.reason
    )
    assert not result.failure.retryable
    if action and failure == "authentication_required":
        assert result.failure.external_text == BROWSER_AUTH_INTERRUPTION_MARKER
    assert len(provider.requests) == 1 and len(provider.actions) == int(action)


@pytest.mark.parametrize("action", [False, True])
async def test_stale_read_recovery_keeps_scope_and_dispatches_once(action: bool) -> None:
    provider = RowProvider()
    provider.stale_once = True
    tool = ShadowBrowserActTool if action else CompletionBrowserObserveTool
    result = await tool(provider).execute(arguments(action), tool_context())
    assert result.ok and result.structured is not None
    assert result.structured["condition"]["status"] == "satisfied"
    assert result.structured["condition"]["observations"] == 4
    assert [r.scope for r in provider.requests] == ["main_document_and_open_shadow"] * 2
    assert provider.observation_count == (1 if action else 2)
    assert len(provider.actions) == int(action)


@pytest.mark.parametrize(
    "rows,truncated,text,expected",
    [
        (["42", "42"], False, "", "ambiguous"),
        (["42"], True, "", "not_observed"),
        ([], False, "", "not_observed"),
        (["42 pending"], False, "", "not_observed"),
        (["42"], False, "Save failed", "ambiguous"),
        ([], False, "Save failed", "failed"),
    ],
)
async def test_inconclusive_rows_and_conflicting_failure_do_not_repeat_action(
    rows: list[str],
    truncated: bool,
    text: str,
    expected: str,
) -> None:
    provider = RowProvider()
    provider.rows, provider.truncated, provider.text = rows, truncated, text
    result = await ShadowBrowserActTool(provider).execute(
        arguments(True, failure_evidence={"kind": "text", "text": "Save failed"}), tool_context()
    )
    assert result.ok and result.structured is not None
    assert result.structured["condition"]["status"] == expected
    assert len(provider.actions) == len(provider.requests) == 1


@pytest.mark.parametrize("action", [False, True])
async def test_cancellation_during_shadow_read_does_not_resend(action: bool) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Provider(RowProvider):
        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            self.requests.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

    provider = Provider()
    tool = ShadowBrowserActTool if action else CompletionBrowserObserveTool
    task = asyncio.create_task(
        tool(provider).execute(arguments(action, timeout_ms=5000), tool_context())
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert len(provider.requests) == 1 and len(provider.actions) == int(action)


@pytest.mark.parametrize("wanted", ["main_document", "main_document_and_open_shadow"])
@pytest.mark.parametrize("actual", ["main_document", "main_document_and_open_shadow"])
async def test_zero_timeout_cached_row_requires_exact_scope(wanted: Any, actual: Any) -> None:
    from agent_core.domain.browser import BrowserCondition
    from agent_core.domain.browser_evidence import BrowserRowEvidence
    from agent_core.tools.browser_conditions import check_browser_condition

    provider = RowProvider()
    evidence = BrowserRowEvidence.model_validate(ROW)
    observed = provider.page(evidence.extraction_request("revision-1", scope=actual))
    result = await check_browser_condition(
        provider,
        observed,
        BrowserCondition(evidence=evidence, timeout_ms=0),
        row_scope=wanted,
    )
    assert result.condition is not None
    assert result.condition.status == ("satisfied" if wanted == actual else "not_observed")
    assert result.condition.observations == 1
    assert not provider.actions and not provider.requests and not provider.observation_count


@pytest.mark.parametrize("action", [False, True])
async def test_legacy_provider_without_extraction_refuses_without_more_actions(
    action: bool,
) -> None:
    provider = FakeBrowserProvider()
    tool = ShadowBrowserActTool if action else CompletionBrowserObserveTool
    result = await tool(provider).execute(arguments(action), tool_context())
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == (
        "tool.browser.outcome_unknown" if action else "tool.browser.action_not_allowed"
    )
    assert not result.failure.retryable and len(provider.actions) == int(action)


async def test_shadow_row_read_deadline_cancels_capture_and_retains_uncertain_effect() -> None:
    stopped = asyncio.Event()

    class Provider(RowProvider):
        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            self.requests.append(request)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            raise AssertionError("unreachable")

    provider = Provider()
    result = await ShadowBrowserActTool(provider).execute(
        arguments(True, timeout_ms=10), tool_context()
    )
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == "tool.browser.outcome_unknown"
    assert not result.failure.retryable and stopped.is_set()
    assert len(provider.requests) == len(provider.actions) == 1
