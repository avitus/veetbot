"""Optional one-call smoke checks against the two credentialed vendors."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.config import PACKAGE_ROOT
from agent_core.domain.messages import (
    ModelAttempt,
    ModelRequest,
    TextPart,
    UserMessage,
)
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from agent_core.model.streaming import collect_turn

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def live_enabled() -> None:
    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set RUN_LIVE_MODEL_TESTS=1 to enable provider smoke tests")


def request(policy: str) -> ModelRequest:
    return ModelRequest(
        model_policy=policy,
        conversation=[UserMessage(content=[TextPart(text="Reply with only: live-ok")])],
        tools=[],
        maximum_output_tokens=64,
    )


def attempt() -> ModelAttempt:
    return ModelAttempt(
        attempt_id=UUID(int=301),
        run_id=UUID(int=302),
        step_number=1,
        attempt_number=1,
        started_at=NOW,
    )


def router() -> StaticModelRouter:
    registry = ProviderRegistry.load(PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS)
    return StaticModelRouter(registry, FixedClock(NOW))


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
async def test_vendor_one_call_smoke(vendor: str) -> None:
    live_enabled()
    environment_name = "OPENAI_API_KEY" if vendor == "openai" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(environment_name)
    if not api_key:
        pytest.skip(f"{environment_name} is absent")
    policy = "balanced" if vendor == "openai" else "flagship"
    resolved = await router().resolve(policy, tenant_id="live")
    provider = (
        OpenAIResponsesProvider(api_key=api_key)
        if vendor == "openai"
        else AnthropicMessagesProvider(api_key=api_key)
    )
    try:
        turn = await collect_turn(provider.stream(request(policy), resolved, attempt()))
    finally:
        await provider.close()
    assert turn.assistant_messages
