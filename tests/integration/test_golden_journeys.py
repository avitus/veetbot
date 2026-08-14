"""Golden user journeys spanning the runtime, tools, provider wire, and public API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any
from uuid import UUID

import httpx

from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.bootstrap import Composition, build
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefType
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.contract.model_fixtures import openai_text_events
from tests.integration.m2_support import database_settings


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    from agent_core.api import create_app

    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(
        app=app,
        client=("127.0.0.1", 43105),
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _run_worker(composition: Composition, worker_id: str) -> None:
    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=composition.clock,
        worker_id=worker_id,
    )
    assert await worker.run_once()


async def _seed_memory(
    composition: Composition,
    session_id: UUID,
    *,
    statement: str,
    subject: str,
) -> None:
    async with composition.uow_factory() as uow:
        source = await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=composition.principal.principal_id,
                payload={"content": statement},
            )
        )
    await composition.memory.remember(
        session_id=session_id,
        run_id=None,
        statement=statement,
        subject=subject,
        scope="general",
        belief_type=BeliefType.FACT,
        source_event_ids=[source.sequence],
    )


def _tool_names(events: list[Any], event_type: str) -> list[str]:
    return [
        str(event.payload["name"])
        for event in events
        if event.event_type == event_type and isinstance(event.payload.get("name"), str)
    ]


async def test_recall_tools_are_followed_by_a_final_assistant_answer() -> None:
    remembered = "User prefers concise status updates."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        arguments={"text": "concise status updates", "scope": "general"},
                        call_id="golden-memory-search",
                    ),
                    ScriptedToolCall(
                        name="memory.recall_episodes",
                        arguments={"text": "concise status updates", "limit": 10},
                        call_id="golden-episode-recall",
                    ),
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                text="You prefer concise status updates.",
                context_contains=remembered,
            ),
        ]
    )
    async with build(
        settings=database_settings(),
        storage="postgres",
        script=script,
    ) as composition:
        session_id = await composition.sessions.create()
        await _seed_memory(
            composition,
            session_id,
            statement=remembered,
            subject="status update preference",
        )
        run_id = await composition.runs.submit(
            "Tell me what you remember about my concise status update preference.",
            session_id,
        )
        await _run_worker(composition, "golden-recall-worker")
        run = await composition.runs.get(run_id)
        events = await composition.runs.events(run_id)

    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "You prefer concise status updates."
    assert set(_tool_names(events, "tool.call.completed")) == {
        "memory.search",
        "memory.recall_episodes",
    }
    assert [event.event_type for event in events][-2:] == [
        "assistant.message.completed",
        "run.completed",
    ]


async def test_recall_tainted_context_allows_an_explicit_user_memory_write() -> None:
    recalled = "User prefers concise status updates."
    explicit = "Deployment region is eu-west-1."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.remember",
                        arguments={
                            "statement": recalled,
                            "subject": "copied recalled preference",
                            "scope": "general",
                        },
                        call_id="golden-recall-derived-write",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
                context_contains=recalled,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.remember",
                        arguments={
                            "statement": explicit,
                            "subject": "deployment region",
                            "scope": "general",
                        },
                        call_id="golden-recall-tainted-write",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I will remember that deployment region."),
        ]
    )
    async with build(
        settings=database_settings(),
        storage="postgres",
        script=script,
    ) as composition:
        seed_session_id = await composition.sessions.create()
        await _seed_memory(
            composition,
            seed_session_id,
            statement=recalled,
            subject="status update preference",
        )
        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit(
            f"Recall my status update preference, and remember exactly: {explicit}",
            session_id,
        )
        await _run_worker(composition, "golden-recall-write-worker")
        run = await composition.runs.get(run_id)
        events = await composition.runs.events(run_id)
        memories = await composition.memory.list_memories()

    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "I will remember that deployment region."
    assert _tool_names(events, "tool.call.failed") == ["memory.remember"]
    assert (
        next(
            event.payload["reason_code"]
            for event in events
            if event.event_type == "tool.call.failed"
        )
        == "tool.output_invalid"
    )
    assert _tool_names(events, "tool.call.completed") == ["memory.remember"]
    assert explicit in {memory.statement for memory in memories}


async def test_invalid_memory_arguments_are_corrected_and_retried_to_success() -> None:
    statement = "Release trains leave on Tuesdays."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.remember",
                        arguments={
                            "statement": statement,
                            "subject": "release train schedule",
                        },
                        call_id="golden-invalid-memory",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.remember",
                        arguments={
                            "statement": statement,
                            "subject": "release train schedule",
                            "scope": "general",
                        },
                        call_id="golden-corrected-memory",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Remembered after correcting the request."),
        ]
    )
    async with build(
        settings=database_settings(),
        storage="postgres",
        script=script,
    ) as composition:
        run_id = await composition.runs.submit(f"Remember exactly: {statement}")
        await _run_worker(composition, "golden-memory-retry-worker")
        run = await composition.runs.get(run_id)
        events = await composition.runs.events(run_id)
        memories = await composition.memory.list_memories()

    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "Remembered after correcting the request."
    assert _tool_names(events, "tool.call.failed") == ["memory.remember"]
    assert (
        next(
            event.payload["reason_code"]
            for event in events
            if event.event_type == "tool.call.failed"
        )
        == "tool.arguments_invalid"
    )
    assert _tool_names(events, "tool.call.completed") == ["memory.remember"]
    assert [memory.statement for memory in memories] == [statement]


async def test_memory_write_is_verified_by_an_independent_later_recall() -> None:
    statement = "The launch marker is ORBIT-7."
    write_script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.remember",
                        arguments={
                            "statement": statement,
                            "subject": "launch marker",
                            "scope": "general",
                        },
                        call_id="golden-durable-memory-write",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Stored the launch marker."),
        ]
    )
    async with build(
        settings=database_settings(),
        storage="postgres",
        script=write_script,
    ) as writer:
        write_run_id = await writer.runs.submit(f"Remember exactly: {statement}")
        await _run_worker(writer, "golden-memory-writer")
        write_run = await writer.runs.get(write_run_id)
    assert write_run.status is RunStatus.COMPLETED

    recall_script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        arguments={"text": "launch marker", "scope": "general"},
                        call_id="golden-independent-recall",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Your launch marker is ORBIT-7.", context_contains=statement),
        ]
    )
    async with build(
        settings=database_settings(),
        storage="postgres",
        script=recall_script,
    ) as reader:
        recall_run_id = await reader.runs.submit("What is my launch marker?")
        await _run_worker(reader, "golden-memory-reader")
        recall_run = await reader.runs.get(recall_run_id)
        recall_events = await reader.runs.events(recall_run_id)

    assert recall_run.status is RunStatus.COMPLETED
    assert recall_run.final_message == "Your launch marker is ORBIT-7."
    assert _tool_names(recall_events, "tool.call.completed") == ["memory.search"]


def _sse_frames(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("id: "):
                fields["id"] = int(line.removeprefix("id: "))
            elif line.startswith("event: "):
                fields["event"] = line.removeprefix("event: ")
            elif line.startswith("data: "):
                fields["data"] = json.loads(line.removeprefix("data: "))
        if "event" in fields:
            frames.append(fields)
    return frames


async def test_final_message_is_delivered_through_the_swift_api_sse_path() -> None:
    final_text = "The durable SSE answer."
    script = FakeModelScript(turns=[ScriptedTurn(text=final_text)])
    async with (
        build(settings=database_settings(), storage="postgres") as api_composition,
        build(
            settings=database_settings(),
            storage="postgres",
            script=script,
        ) as worker_composition,
        _client(api_composition) as client,
    ):
        created = await client.post(
            "/v1/sessions",
            json={"agent_id": "general", "metadata": {}},
        )
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"Idempotency-Key": "golden-swift-sse"},
            json={"content": [{"type": "text", "text": "Give me the SSE answer."}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = UUID(submitted.json()["run_id"])
        await _run_worker(worker_composition, "golden-swift-sse-worker")
        stream = await client.get(f"/v1/runs/{run_id}/events")
        run = await client.get(f"/v1/runs/{run_id}")

    assert stream.status_code == 200, stream.text
    assert run.status_code == 200, run.text
    assert run.json()["status"] == RunStatus.COMPLETED.value
    frames = _sse_frames(stream.text)
    durable_message = next(
        frame for frame in frames if frame["event"] == "assistant.message.completed"
    )
    terminal = next(frame for frame in frames if frame["event"] == "run.completed")
    assert durable_message["data"]["message"]["content"] == [{"kind": "text", "text": final_text}]
    assert terminal["data"]["final_message"]["content"] == [{"kind": "text", "text": final_text}]
    assert durable_message["id"] < terminal["id"]


class _CapturingResponses:
    def __init__(self, streams: list[list[dict[str, Any]]]) -> None:
        self._streams = streams
        self.requests: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> AsyncIterator[dict[str, Any]]:
        self.requests.append(deepcopy(payload))
        events = deepcopy(self._streams[len(self.requests) - 1])
        if len(self.requests) == 1:
            wire_name = str(payload["tools"][0]["name"])
            for event in events:
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    item["name"] = wire_name

        async def stream() -> AsyncIterator[dict[str, Any]]:
            for event in events:
                yield event

        return stream()


class _CapturingOpenAIClient:
    def __init__(self, streams: list[list[dict[str, Any]]]) -> None:
        self.responses = _CapturingResponses(streams)


def _openai_reasoning_tool_events() -> list[dict[str, Any]]:
    return [
        {
            "type": "response.created",
            "response": {"id": "resp-golden-reasoning-tool"},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "delta": "checking the calculation",
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "reasoning",
                "id": "golden-reasoning-item",
                "encrypted_content": "opaque-golden-reasoning-state",
                "status": "completed",
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "call_id": "golden-openai-tool-call",
                "name": "replaced-at-sdk-boundary",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": '{"expression":"17 * 23"}',
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp-golden-reasoning-tool",
                "model": "gpt-golden",
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


async def test_openai_reasoning_and_tool_replay_use_the_serialized_sdk_request_path() -> None:
    client = _CapturingOpenAIClient([_openai_reasoning_tool_events(), openai_text_events("391")])
    provider = OpenAIResponsesProvider(client=client)
    async with build(
        settings=database_settings(),
        storage="memory",
        model_policy="balanced",
        model_provider_overrides={"openai": provider},
        enabled_tools=["math.calculate"],
    ) as composition:
        run_id = await composition.runs.submit("Calculate 17 * 23 with reasoning.")
        run = await composition.runs.get(run_id)

    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "391"
    assert len(client.responses.requests) == 2
    first, second = client.responses.requests
    assert first["stream"] is True
    assert second["stream"] is True
    assert first["store"] is False
    assert second["store"] is False
    replay = second["input"]
    reasoning = next(item for item in replay if item.get("type") == "reasoning")
    function_call = next(item for item in replay if item.get("type") == "function_call")
    function_result = next(item for item in replay if item.get("type") == "function_call_output")
    assert reasoning == {
        "id": "golden-reasoning-item",
        "type": "reasoning",
        "encrypted_content": "opaque-golden-reasoning-state",
        "status": "completed",
        "summary": [],
    }
    assert function_call["call_id"] == "golden-openai-tool-call"
    assert function_call["arguments"] == '{"expression":"17 * 23"}'
    assert function_result["call_id"] == "golden-openai-tool-call"
    assert "391" in function_result["output"]
    assert replay.index(reasoning) < replay.index(function_call) < replay.index(function_result)
