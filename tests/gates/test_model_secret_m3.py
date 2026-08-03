"""Captured model surfaces never receive credentials or raw provider bodies."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.persistence.mappers import model_call_values
from agent_core.domain.messages import (
    ModelAttempt,
    ModelRequest,
    ModelUsage,
    ProviderMetadata,
    ResolvedModel,
    TextPart,
    UserMessage,
)
from agent_core.domain.persistence import ModelCallRecord
from agent_core.observability.models import span_provider_attributes
from agent_core.ports.models import ModelProvider
from tests.contract.model_fixtures import ScriptedRawSource
from tests.contract.support import NOW, RUN_ID, SESSION_ID, TENANT


def request() -> ModelRequest:
    return ModelRequest(
        model_policy="secret-gate",
        conversation=[UserMessage(content=[TextPart(text="safe request")])],
        tools=[],
    )


def resolved(provider: str) -> ResolvedModel:
    return ResolvedModel(provider=provider, model="safe-model", resolved_at=NOW)


ATTEMPT = ModelAttempt(
    attempt_id=UUID(int=401),
    run_id=RUN_ID,
    step_number=1,
    attempt_number=1,
    started_at=NOW,
)


async def serialized_events(provider: ModelProvider, provider_name: str) -> str:
    events = []
    async for event in provider.stream(request(), resolved(provider_name), ATTEMPT):
        events.append(event.model_dump(mode="json"))
    await provider.close()
    return json.dumps(events, sort_keys=True)


async def test_scanner_over_events_logs_spans_and_rows_finds_no_raw_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    provider_key = "sk-" + "s" * 16
    authorization = "Authorization:" + " Bearer " + provider_key
    raw_body = "vendor rejected request containing " + authorization
    openai = OpenAIResponsesProvider(
        event_source=ScriptedRawSource(
            [
                [
                    {
                        "type": "response.failed",
                        "response": {"error": {"code": raw_body, "message": raw_body}},
                    }
                ]
            ]
        )
    )
    anthropic = AnthropicMessagesProvider(
        event_source=ScriptedRawSource(
            [[{"type": "error", "error": {"type": raw_body, "message": raw_body}}]]
        )
    )

    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=raw_body)

    chat_client = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    chat = ChatCompletionsProvider(
        base_url="http://127.0.0.1:11434/v1",
        client=chat_client,
    )
    event_surfaces = "".join(
        [
            await serialized_events(openai, "openai"),
            await serialized_events(anthropic, "anthropic"),
            await serialized_events(chat, "chat_completions"),
        ]
    )
    await chat_client.aclose()

    metadata = ProviderMetadata(
        provider_api="responses",
        response_id="safe-response",
        request_id="safe-request",
        resolved_model="safe-model",
    )
    row = model_call_values(
        ModelCallRecord(
            attempt_id=ATTEMPT.attempt_id,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            tenant_id=TENANT,
            step_number=1,
            attempt_number=1,
            provider="openai",
            model="safe-model",
            model_policy="balanced",
            registry_version="safe-registry",
            prefix_sha256="0" * 64,
            usage=ModelUsage(),
            cost=Decimal("0"),
            cost_source=ModelUsage().cost_source,
            metadata=metadata,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    captured = json.dumps(
        {
            "events": event_surfaces,
            "logs": caplog.text,
            "span": span_provider_attributes(metadata),
            "row": row,
        },
        default=str,
        sort_keys=True,
    )
    assert provider_key not in captured
    assert authorization not in captured
    assert raw_body not in captured
