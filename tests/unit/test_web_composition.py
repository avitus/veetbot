"""Composition-root selection for capability-level web providers."""

from __future__ import annotations

from typing import cast

import pytest

from agent_core.adapters.web.firecrawl import FirecrawlWebProvider
from agent_core.adapters.web.tavily import TavilyWebProvider
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.tools.registry import RegisteredTool
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_search import WebSearchTool
from tests.unit.test_config import base_environment
from tests.unit.test_web_tools import FakeWebProvider


async def test_recommended_hybrid_registers_tavily_search_and_firecrawl_fetch() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "WEB_SEARCH_PROVIDER": "tavily",
            "WEB_FETCH_PROVIDER": "firecrawl",
            "TAVILY_API_KEY": "synthetic-tavily-credential",
            "FIRECRAWL_API_KEY": "synthetic-firecrawl-credential",
        }
    )

    async with build(settings=settings, enabled_tools=["web.search", "web.fetch"]) as composition:
        registry = composition.tool_pipeline._registry
        search = cast(RegisteredTool, registry.get("web.search"))
        fetch = cast(RegisteredTool, registry.get("web.fetch"))
        assert isinstance(search.implementation, WebSearchTool)
        assert isinstance(fetch.implementation, WebFetchTool)
        assert isinstance(search.implementation._provider, TavilyWebProvider)
        assert isinstance(fetch.implementation._provider, FirecrawlWebProvider)


@pytest.mark.parametrize(
    ("provider_name", "provider_type"),
    [
        ("tavily", TavilyWebProvider),
        ("firecrawl", FirecrawlWebProvider),
    ],
)
async def test_one_provider_can_serve_both_web_capabilities(
    provider_name: str,
    provider_type: type[object],
) -> None:
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "WEB_SEARCH_PROVIDER": provider_name,
            "WEB_FETCH_PROVIDER": provider_name,
            f"{provider_name.upper()}_API_KEY": "synthetic-provider-credential",
        }
    )

    async with build(settings=settings, enabled_tools=["web.search", "web.fetch"]) as composition:
        registry = composition.tool_pipeline._registry
        search = cast(RegisteredTool, registry.get("web.search")).implementation
        fetch = cast(RegisteredTool, registry.get("web.fetch")).implementation
        assert isinstance(search, WebSearchTool)
        assert isinstance(fetch, WebFetchTool)
        assert isinstance(search._provider, provider_type)
        assert fetch._provider is search._provider


async def test_disabled_web_capabilities_are_not_registered() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(settings=settings) as composition:
        registry = composition.tool_pipeline._registry
        with pytest.raises(NotFoundError):
            registry.get("web.search")
        with pytest.raises(NotFoundError):
            registry.get("web.fetch")


async def test_web_search_runs_through_policy_and_persists_untrusted_result() -> None:
    provider = FakeWebProvider()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="web.search",
                        arguments={"query": "Ada Lovelace", "max_results": 5},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I found a public source.", stop_reason=StopReason.END_TURN),
        ]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings,
        script=script,
        sequential_ids=True,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Find public information about Ada Lovelace.")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert run.final_message == "I found a public source."
    assert provider.searches[0].query == "Ada Lovelace"
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.SUCCEEDED
    assert invocations[0].result_item is not None
    assert invocations[0].result_item.trust is TrustLevel.EXTERNAL_UNTRUSTED


async def test_web_research_plan_omits_unusable_skill_loader() -> None:
    provider = FakeWebProvider()
    script = FakeModelScript(
        turns=[ScriptedTurn(text="No tool call needed.", stop_reason=StopReason.END_TURN)]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings,
        script=script,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit(
            "Search for publicly available information about Ada Lovelace.", session_id
        )
        await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            plan = await uow.events.latest_before(
                session_id,
                (1 << 63) - 1,
                "context.plan.created",
                composition.principal,
            )

    assert plan is not None
    tool_names = plan.payload["plan"]["tool_names"]
    assert "web.search" in tool_names
    assert "web.fetch" in tool_names
    assert "skill.load" not in tool_names
