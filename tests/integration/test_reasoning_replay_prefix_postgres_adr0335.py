"""A tool loop keeps its replayed reasoning on the real wire, from PostgreSQL (ADR-0335).

Guards ADR-0335's first consequence end to end: "Between two steps the only
changes before the replayed reasoning are whole items leaving." GPT-6.2 drops
a replayed reasoning item when anything before it was inserted or edited
since the request that produced it, without an error.

The unit suite proves the renderer and a checkpoint assembled by hand. Here a
chat on the GPT-6.2 Astra policy runs a two-call tool loop through the durable
worker, its conversation is persisted to and read back from the PostgreSQL
event log (which assigns the sequences the nonces derive from), and the
serialized Responses requests are compared as the provider would see them.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.bootstrap import build
from agent_core.domain.runs import RunStatus
from tests.contract.model_fixtures import openai_text_events
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration


def _reasoning_then_tool(step: int, expression: str) -> list[dict[str, Any]]:
    """One model turn: an encrypted reasoning item, then one calculator call."""
    return [
        {"type": "response.created", "response": {"id": f"resp-{step}"}},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "reasoning",
                "id": f"reasoning-{step}",
                "encrypted_content": f"opaque-reasoning-state-{step}",
                "status": "completed",
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"type": "function_call", "call_id": f"call-{step}", "name": "replaced"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": json.dumps({"expression": expression}),
        },
        {
            "type": "response.completed",
            "response": {
                "id": f"resp-{step}",
                "model": "gpt-6.2-astra",
                "status": "completed",
                "usage": {
                    "input_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 8,
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            },
        },
    ]


class _Wire:
    """Answer each Responses request from a script and keep what was sent."""

    def __init__(self, streams: list[list[dict[str, Any]]]) -> None:
        self._streams = streams
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        self.requests.append(payload)
        assert len(self.requests) <= len(self._streams), "unexpected extra provider request"
        events = deepcopy(self._streams[len(self.requests) - 1])
        for event in events:
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                # The provider is shown the tool under its wire spelling.
                item["name"] = str(payload["tools"][0]["name"])
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body, request=request
        )


def _model_visible(request: dict[str, Any]) -> list[str]:
    """Each input item as the bytes the model reads.

    Cache annotations sit outside the model-visible prefix and may move. Only
    their presence is dropped, with the one-block wrapping an annotated text
    needs; message bytes, envelopes and reasoning items are compared as sent.
    """
    rows: list[str] = []
    for item in deepcopy(request["input"]):
        for field in ("content", "output"):
            blocks = item.get(field)
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict):
                        block.pop("prompt_cache_breakpoint", None)
                if len(blocks) == 1 and set(blocks[0]) == {"type", "text"}:
                    item[field] = blocks[0]["text"]
        rows.append(json.dumps(item, sort_keys=True))
    return rows


def _reasoning_index(request: dict[str, Any], reasoning_id: str) -> int:
    [index] = [
        position
        for position, item in enumerate(request["input"])
        if item.get("type") == "reasoning" and item.get("id") == reasoning_id
    ]
    return index


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    remaining = iter(haystack)
    return all(row in remaining for row in needle)


async def test_only_whole_items_leave_before_the_replayed_reasoning_across_tool_steps() -> None:
    wire = _Wire(
        [
            _reasoning_then_tool(1, "17 * 23"),
            _reasoning_then_tool(2, "391 + 9"),
            openai_text_events("400"),
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as http_client:
        provider = OpenAIResponsesProvider(
            client=AsyncOpenAI(
                api_key="test",
                base_url="https://openai.test/v1",
                http_client=http_client,
                max_retries=0,
            )
        )
        async with (
            build(
                settings=database_settings(),
                storage="postgres",
                model_policy="astra-6-2",
                model_provider_overrides={"openai": provider},
                enabled_tools=["math.calculate"],
            ) as composition,
            api_client(composition) as client,
        ):
            created = await client.post(
                "/v1/sessions", json={"agent_id": "general", "metadata": {}}
            )
            assert created.status_code == 201, created.text
            submitted = await client.post(
                f"/v1/sessions/{created.json()['id']}/messages",
                headers={"Idempotency-Key": "reasoning-replay"},
                json={"content": [{"type": "text", "text": "Multiply 17 by 23, then add 9."}]},
            )
            assert submitted.status_code == 202, submitted.text
            assert await run_worker_once(composition, "reasoning-replay")
            run = await client.get(f"/v1/runs/{submitted.json()['run_id']}")

    assert run.json()["status"] == RunStatus.COMPLETED.value, run.text
    first, second, third = wire.requests
    assert first["model"] == second["model"] == third["model"] == "gpt-6.2-astra"
    # Each step replays the reasoning of the step before it, ahead of its call.
    for request, reasoning, call in (
        (second, "reasoning-1", "call-1"),
        (third, "reasoning-2", "call-2"),
    ):
        types = [(item.get("type"), item.get("call_id")) for item in request["input"]]
        assert _reasoning_index(request, reasoning) < types.index(("function_call", call))
        assert ("function_call_output", call) in types
    # The spent reasoning leaves whole; the call and result it led to stay.
    assert not [item for item in third["input"] if item.get("id") == "reasoning-1"]
    assert ("function_call_output", "call-1") in [
        (item.get("type"), item.get("call_id")) for item in third["input"]
    ]

    # ADR-0335: nothing before the replayed reasoning was inserted or edited
    # since the request that produced it. Every such item was sent, byte for
    # byte and in the same order, in the previous request.
    for earlier, later, reasoning in (
        (first, second, "reasoning-1"),
        (second, third, "reasoning-2"),
    ):
        before = _model_visible(later)[: _reasoning_index(later, reasoning)]
        assert before, "the reasoning item follows the conversation it was produced from"
        assert _is_subsequence(before, _model_visible(earlier))
    # The tool result that precedes the second step's reasoning is one of them.
    result_row = next(
        row
        for row, item in zip(_model_visible(third), third["input"], strict=True)
        if (item.get("type"), item.get("call_id")) == ("function_call_output", "call-1")
    )
    assert result_row in _model_visible(second)
