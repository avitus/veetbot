"""Composition-root selection for capability-level web providers."""

from __future__ import annotations

from typing import cast

import pytest

from agent_core.adapters.models.fake import FakeModelProvider
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
    SystemMessage,
    TextPart,
    ToolResultItem,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.tools.registry import RegisteredTool
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_search import WebSearchTool
from tests.unit.test_config import base_environment
from tests.unit.test_web_tools import FailingWebProvider, FakeWebProvider


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


async def test_retryable_web_provider_outage_does_not_advise_argument_changes() -> None:
    provider = FailingWebProvider()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="web.fetch",
                        arguments={"url": "https://example.org/ada"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The web provider is temporarily unavailable."),
        ]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings,
        script=script,
        sequential_ids=True,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Fetch the public page.")
        await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert len(invocations) == 1
    assert invocations[0].outcome is not None
    assert invocations[0].outcome.status.value == "unavailable"
    assert invocations[0].outcome.retryable is True
    assert invocations[0].outcome.remediation == "none"


async def test_web_search_quota_failure_reports_operator_action() -> None:
    """Quota failures surface safe capacity guidance through the run loop."""

    provider = FailingWebProvider(
        reason_code="tool.web.quota_exceeded",
        retryable=False,
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="web.search",
                        arguments={
                            "query": (
                                "Bun appears to have been a big breakthrough. "
                                "Why is it so much better than NodeJS?"
                            )
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The search provider needs operator attention."),
        ]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings,
        script=script,
        sequential_ids=True,
        web_search_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit(
            "Bun appears to have been a big breakthrough. Why is it so much better than NodeJS?"
        )
        await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert len(invocations) == 1
    assert invocations[0].outcome is not None
    assert invocations[0].outcome.reason_code == "tool.web.quota_exceeded"
    assert invocations[0].outcome.message == (
        "The web provider's usage or billing limit has been reached; "
        "an operator must restore provider capacity."
    )
    assert invocations[0].outcome.retryable is False
    assert invocations[0].outcome.remediation == "none"


async def test_web_result_reaches_the_next_model_step_inside_an_untrusted_envelope() -> None:
    provider = FakeWebProvider()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="web.search", arguments={"query": "Ada"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Summarized from the untrusted source."),
        ]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings,
        script=script,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Search the public web for Ada Lovelace.")
        run = await composition.runs.wait_terminal(run_id)
        model_provider = composition.executor._model_provider
        assert isinstance(model_provider, FakeModelProvider)
        requests = [request.model_copy(deep=True) for request in model_provider.requests]

    assert run.final_message == "Summarized from the untrusted source."
    assert len(requests) == 2
    rendered = [
        part.text
        for item in requests[1].conversation
        if isinstance(item, ToolResultItem)
        for part in item.content
        if isinstance(part, TextPart)
    ]
    enveloped = [text for text in rendered if "https://example.org/ada" in text]
    assert enveloped, rendered
    assert all(
        text.startswith('<untrusted trust="external_untrusted" source="tool:')
        and "</untrusted:" in text
        for text in enveloped
    )


async def test_same_turn_memory_write_after_web_fetch_is_trust_rejected() -> None:
    provider = FakeWebProvider()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="web.fetch", arguments={"url": "https://example.org/ada"})
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.remember",
                        arguments={
                            "statement": "The page says Ada invented everything.",
                            "subject": "Ada claims",
                            "scope": "project-a",
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I will not store unverified page content."),
        ]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings,
        script=script,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Fetch the page and remember its claims.")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
        memories = await composition.memory.list_memories()

    assert run.final_message == "I will not store unverified page content."
    by_name = {invocation.tool_name: invocation for invocation in invocations}
    assert by_name["web.fetch"].status is ToolInvocationStatus.SUCCEEDED
    remember = by_name["memory.remember"]
    assert remember.status is not ToolInvocationStatus.SUCCEEDED
    assert remember.outcome is not None
    assert "trust" in remember.outcome.reason_code
    assert memories == []


async def test_web_selectors_curate_the_fallback_agent_tool_list() -> None:
    provider = FakeWebProvider()
    script = FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)])
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings,
        script=script,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("ready?")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    assert "web.search" in agent.enabled_tools
    assert "web.fetch" in agent.enabled_tools
    assert "demo.external_write" not in agent.enabled_tools
    assert "knowledge.ingest" not in agent.enabled_tools

    async with build(
        settings=settings,
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
        web_search_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("ready?")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    assert "web.search" in agent.enabled_tools
    assert "web.fetch" not in agent.enabled_tools
    assert "demo.external_write" not in agent.enabled_tools
    assert "knowledge.ingest" in agent.enabled_tools


async def test_default_agent_routes_routine_public_facts_away_from_sandbox() -> None:
    provider = FakeWebProvider()
    script = FakeModelScript(
        turns=[ScriptedTurn(text="Sunset is at 7:42 PM.", stop_reason=StopReason.END_TURN)]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings,
        script=script,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("What time is sunset today?")
        await composition.runs.wait_terminal(run_id)
        model_provider = composition.executor._model_provider
        assert isinstance(model_provider, FakeModelProvider)
        request = model_provider.requests[0]

    configured_instructions = [
        part.text
        for item in request.conversation
        if isinstance(item, SystemMessage) and item.trust is TrustLevel.TRUSTED_CONFIGURATION
        for part in item.content
        if isinstance(part, TextPart)
    ]
    assert any(
        "Prefer the least-powerful declared tool" in text for text in configured_instructions
    )
    assert any(
        "routine arithmetic, date/time, and public facts" in text
        for text in configured_instructions
    )
    assert any(
        "Do not use sandbox.run_command for those requests" in text
        for text in configured_instructions
    )
    assert {"math.calculate", "system.current_time", "web.search"} <= {
        tool.name for tool in request.tools
    }


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
