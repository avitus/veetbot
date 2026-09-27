"""Composed runtime-to-provider synthesis checks; no database or live credentials."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.bootstrap import build
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.ports.models import ModelProvider
from tests.contract.model_fixtures import (
    anthropic_text_events,
    anthropic_tool_events,
    chat_text_events,
    chat_tool_events,
    openai_text_events,
    openai_tool_events,
)
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _settings


@pytest.mark.parametrize("adapter", ["openai", "anthropic", "chat_completions"])
async def test_real_adapter_loop_synthesizes_before_repeated_call_failure(adapter: str) -> None:
    """The application, runtime and real request encoder agree on the final turn."""

    requests: list[dict[str, Any]] = []
    tool_events = {
        "openai": openai_tool_events,
        "anthropic": anthropic_tool_events,
        "chat_completions": chat_tool_events,
    }[adapter]
    text_events = {
        "openai": openai_text_events,
        "anthropic": anthropic_text_events,
        "chat_completions": chat_text_events,
    }[adapter]

    async def source(payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        requests.append(payload)
        disabled = payload.get("tool_choice") in ("none", {"type": "none"})
        if adapter == "chat_completions":
            # The shipped local policy uses the XML fallback, not native tools.
            disabled = "Available tool schemas" not in str(payload)
        events = (
            text_events("Findings are partial; research repeated.")
            if disabled
            else tool_events('{"expression":"17 * 23"}', call_id=f"read-{len(requests)}")
        )
        for event in events:
            yield event

    provider: ModelProvider
    if adapter == "openai":
        provider = OpenAIResponsesProvider(event_source=source)
    elif adapter == "anthropic":
        provider = AnthropicMessagesProvider(event_source=source)
    else:
        provider = ChatCompletionsProvider(
            base_url="http://127.0.0.1:11434/v1", event_source=source
        )

    async with build(
        settings=_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        model_policy={"openai": "balanced", "anthropic": "flagship", "chat_completions": "local"}[
            adapter
        ],
        model_provider_overrides={adapter: provider},
        limits=RunLimits(max_steps=12, max_model_calls=12, max_tool_calls=20),
    ) as composition:
        run_id = await composition.runs.submit("Research and report what you found.")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.final_message == "Findings are partial; research repeated."
    assert len(requests) == 5
    assert all("tool_choice" not in request for request in requests[:-1])
    if adapter == "chat_completions":
        assert "Available tool schemas" in str(requests[0])
        assert "Available tool schemas" not in str(requests[-1])
        assert all("tools" not in request for request in requests)
    else:
        assert requests[-1]["tools"] == requests[0]["tools"]
    assert [event.event_type for event in events].count("tool.call.completed") == 4
    assert [event.event_type for event in events].count("run.completed") == 1
