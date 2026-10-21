"""A saved login without an open page needs navigation, not another sign-in."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.domain.browser import (
    BrowserObservation,
    BrowserObservationExpansion,
    BrowserProviderError,
)
from agent_core.domain.browser_diagnostics import browser_failure_category
from agent_core.domain.browser_extraction import BrowserExtractionField, BrowserExtractionRequest
from agent_core.domain.messages import TextPart, ToolCallItem
from agent_core.domain.runs import Step
from agent_core.domain.tools import ToolOutcome
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.browser_observe import BrowserObserveTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW, agent, principal, run
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_history_cache_window import _checkpoint, _factory


async def read(runtime: PythonPlaywrightRuntime, operation: str) -> BrowserObservation:
    """Exercise each browser read operation against the same page state."""

    if operation == "expand":
        return await runtime.expand(BrowserObservationExpansion(after="old-control"))
    if operation == "extract":
        return await runtime.extract(
            BrowserExtractionRequest(
                expected_revision="old-revision",
                kind="table",
                fields=[BrowserExtractionField(name="label", column=0, type="string")],
            )
        )
    return await runtime.observe()


@pytest.mark.parametrize("operation", ["observe", "expand", "extract"])
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("about:blank", "tool.browser.navigation_required"),
        ("https://other.example/", "tool.browser.profile_unavailable"),
        (None, "tool.browser.profile_unavailable"),
    ],
)
async def test_read_distinguishes_blank_page_from_unavailable_profile(
    operation: str, url: str | None, reason: str
) -> None:
    """An unopened page needs navigation; absent or foreign pages remain unavailable."""

    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://example.org",)
    if url is not None:
        runtime._page = Mock(url=url)

    with pytest.raises(BrowserProviderError) as refused:
        await read(runtime, operation)

    assert refused.value.reason_code == reason
    assert not refused.value.retryable


async def test_blank_page_tool_result_tells_model_to_navigate_without_reauthentication() -> None:
    """Expose an actionable navigation result through the actual tool pipeline."""

    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://example.org",)
    runtime._page = Mock(url="about:blank")

    class Provider(FakeBrowserProvider):
        async def observe(self) -> BrowserObservation:
            """Delegate observation to the runtime whose page has not navigated yet."""

            return await runtime.observe()

    clock, factory = await _factory()
    registry = StaticToolRegistry()
    tool = BrowserObserveTool(Provider())
    registry.register(tool)
    pipeline = ToolPipeline(registry, factory, clock, SequenceIdFactory())
    active = run()
    async with factory() as uow:
        await uow.runs.create(active)
    results = await pipeline.dispatch(
        run=active,
        checkpoint=_checkpoint(active, []),
        tool_calls=[
            ToolCallItem(
                call_id="observe-resumed",
                item_index=0,
                name=tool.spec.name,
                arguments={},
                raw_arguments="{}",
            )
        ],
        principal=principal(),
        step=Step(run_id=active.id, step_number=1, started_at=NOW),
        agent=agent().model_copy(update={"enabled_tools": [tool.spec.name]}),
        token=RunCancellationToken(clock, None),
    )

    content = results[0].content[0]
    assert isinstance(content, TextPart)
    outcome = ToolOutcome.model_validate_json(content.text)
    assert outcome.reason_code == "tool.browser.navigation_required"
    assert "browser.navigate" in outcome.message
    assert "allowed" in outcome.message
    assert "sign in again" in outcome.message
    assert "Never replay" in outcome.message
    assert not outcome.retryable
    assert browser_failure_category(outcome.reason_code) == "stale_page"
