"""Deterministic capability-level web-provider routing."""

from __future__ import annotations

from agent_core.adapters.web.routing import WeightedWebProviderRouter
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_search import WebSearchTool
from tests.contract.support import tool_context
from tests.unit.test_web_tools import FailingWebProvider, FakeWebProvider


async def test_web_tools_report_the_actual_routed_provider_on_success_and_failure() -> None:
    incumbent = FakeWebProvider(name="incumbent")
    keenable = FailingWebProvider(
        name="keenable",
        reason_code="tool.web.provider_unavailable",
        retryable=True,
    )
    successful_router = WeightedWebProviderRouter(
        ((incumbent, 50), (keenable, 50)),
        bucket_for_key=lambda key: 25,
    )
    failing_router = WeightedWebProviderRouter(
        ((incumbent, 50), (keenable, 50)),
        bucket_for_key=lambda key: 75,
    )

    search = await WebSearchTool(successful_router).execute({"query": "Ada"}, tool_context())
    fetch = await WebFetchTool(failing_router).execute(
        {"url": "https://example.org/ada"},
        tool_context(),
    )

    assert search.ok
    assert search.structured is not None
    assert search.structured["provider"] == "incumbent"
    assert not fetch.ok
    assert fetch.structured == {"provider": "keenable"}
