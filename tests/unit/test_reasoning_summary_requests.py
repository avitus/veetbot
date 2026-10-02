"""A Chat run asks for a displayable reasoning summary; others never do (ADR-0144)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from openai import APIStatusError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.bootstrap import build
from agent_core.domain.messages import (
    FakeModelScript,
    ModelCompletedEvent,
    ReasoningEffort,
    ReasoningSupport,
    ScriptedTurn,
)
from agent_core.model.streaming import validated_stream
from tests.contract.model_fixtures import openai_text_events
from tests.contract.support import NOW
from tests.contract.test_model_gateway_contract import ATTEMPT, request, resolved
from tests.gates.test_model_settings_api_adr0119 import _principal, _settings


async def test_a_chat_run_asks_for_a_reasoning_summary() -> None:
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="Hello.")]), FixedClock(NOW)
    )
    async with build(
        settings=_settings("openai", "anthropic"),
        storage="memory",
        sequential_ids=True,
        model_policy="astra",
        principal=_principal("session.read"),
        model_provider_overrides={"openai": provider, "anthropic": provider},
    ) as composition:
        await composition.runs.wait_terminal(await composition.runs.submit("Hello."))

    assert [request.reasoning_summary for request in provider.requests] == [True]


def test_openai_asks_for_an_automatic_summary_with_the_effort() -> None:
    payload = OpenAIResponsesProvider._request_payload(
        request().model_copy(
            update={"reasoning_effort": ReasoningEffort.HIGH, "reasoning_summary": True}
        ),
        resolved("openai"),
    )

    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}


def test_openai_asks_for_a_summary_at_the_default_effort() -> None:
    payload = OpenAIResponsesProvider._request_payload(
        request().model_copy(update={"reasoning_summary": True}), resolved("openai")
    )

    assert payload["reasoning"] == {"summary": "auto"}


def test_a_summary_is_never_requested_without_native_reasoning() -> None:
    model = resolved("openai")
    without_reasoning = model.model_copy(
        update={
            "capabilities": model.capabilities.model_copy(
                update={"reasoning": ReasoningSupport.NONE}
            )
        }
    )

    assert "reasoning" not in OpenAIResponsesProvider._request_payload(
        request(), resolved("openai")
    )
    assert "reasoning" not in OpenAIResponsesProvider._request_payload(
        request().model_copy(update={"reasoning_summary": True}), without_reasoning
    )


async def test_an_account_that_cannot_summarize_still_gets_its_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OpenAI refuses summaries to unverified organizations; the answer must not fail."""

    sent: list[dict[str, Any]] = []

    async def source(payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        sent.append(payload)
        if "summary" in payload["reasoning"]:
            raise APIStatusError(
                "organization must be verified",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://api.openai.com/v1/responses")
                ),
                body={"error": {"code": "unsupported_value", "param": "reasoning.summary"}},
            )
        for event in openai_text_events("Hello."):
            yield event

    caplog.set_level("WARNING", logger="agent_core.adapters.models.openai_responses")
    provider = OpenAIResponsesProvider(event_source=source)
    summarizing = request().model_copy(
        update={"reasoning_effort": ReasoningEffort.HIGH, "reasoning_summary": True}
    )
    try:
        first = [
            event
            async for event in validated_stream(
                provider.stream(summarizing, resolved("openai"), ATTEMPT)
            )
        ]
        second = [
            event
            async for event in validated_stream(
                provider.stream(summarizing, resolved("openai"), ATTEMPT)
            )
        ]
    finally:
        await provider.close()

    assert isinstance(first[-1], ModelCompletedEvent)
    assert isinstance(second[-1], ModelCompletedEvent)
    assert [payload["reasoning"] for payload in sent] == [
        {"effort": "high", "summary": "auto"},
        {"effort": "high"},
        {"effort": "high"},
    ]
    assert caplog.text.count("openai_reasoning_summary_refused") == 1


async def test_a_summary_refusal_on_the_last_attempt_still_gets_its_answer() -> None:
    """The optional summary must not spend the retry budget the answer needs."""

    sent: list[dict[str, Any]] = []

    async def source(payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        sent.append(payload)
        if "summary" in payload["reasoning"]:
            raise APIStatusError(
                "organization must be verified",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://api.openai.com/v1/responses")
                ),
                body={"error": {"code": "unsupported_value", "param": "reasoning.summary"}},
            )
        for event in openai_text_events("Hello."):
            yield event

    provider = OpenAIResponsesProvider(event_source=source, max_internal_attempts=1)
    summarizing = request().model_copy(
        update={"reasoning_effort": ReasoningEffort.HIGH, "reasoning_summary": True}
    )
    try:
        events = [
            event
            async for event in validated_stream(
                provider.stream(summarizing, resolved("openai"), ATTEMPT)
            )
        ]
    finally:
        await provider.close()

    assert isinstance(events[-1], ModelCompletedEvent)
    assert [payload["reasoning"] for payload in sent] == [
        {"effort": "high", "summary": "auto"},
        {"effort": "high"},
    ]
