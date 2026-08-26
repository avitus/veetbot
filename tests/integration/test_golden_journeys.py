"""Golden user journeys spanning the runtime, tools, provider wire, and public API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime
from datetime import time as civil_time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
from openai import AsyncOpenAI

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.push import FakePushTransport
from agent_core.application.notification_dispatcher import NotificationDispatcher
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import AgentSpec
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.devices import PushProvider
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefType
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from agent_core.scheduling.worker import ScheduleWorker
from tests.contract.model_fixtures import openai_text_events
from tests.integration.m2_support import database_settings, memory_settings


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
    # The answer names no belief identifier, so completion closes with the
    # usage feedback for what the journey recalled and did not cite.
    assert [event.event_type for event in events][-4:] == [
        "assistant.message.completed",
        "run.completed",
        "memory.formation.requested",
        "memory.cited",
    ]


async def test_recalled_memory_context_allows_an_explicit_user_memory_write() -> None:
    recalled = "User prefers concise status updates."
    explicit = "I use Vim."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.remember",
                        arguments={
                            "statement": explicit,
                            "subject": "editor preference",
                            "scope": "general",
                        },
                        call_id="golden-recalled-memory-write",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
                context_contains=recalled,
            ),
            ScriptedTurn(text="I will remember that editor preference."),
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
            "Recall my status update preference, and remember that my editor is Vim.",
            session_id,
        )
        await _run_worker(composition, "golden-recall-write-worker")
        run = await composition.runs.get(run_id)
        events = await composition.runs.events(run_id)
        memories = await composition.memory.list_memories()

    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "I will remember that editor preference."
    assert _tool_names(events, "tool.call.failed") == []
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
    failed_payload = next(
        event.payload for event in events if event.event_type == "tool.call.failed"
    )
    assert failed_payload["reason_code"] == "tool.arguments_invalid"
    failed_outcome = json.loads(failed_payload["result_item"]["content"][0]["text"])
    assert failed_outcome["remediation"] == "modify_arguments"
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
        stream = await client.get(f"/v1/runs/{run_id}/events", timeout=10.0)
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


class _OpenAIWire:
    def __init__(self, streams: list[list[dict[str, Any]]]) -> None:
        self._streams = streams
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        self.requests.append(payload)
        assert len(self.requests) <= len(self._streams), "unexpected extra provider request"
        events = deepcopy(self._streams[len(self.requests) - 1])
        if len(self.requests) == 1:
            wire_name = str(payload["tools"][0]["name"])
            for event in events:
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    item["name"] = wire_name
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        body += "data: [DONE]\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
            request=request,
        )


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
    wire = _OpenAIWire([_openai_reasoning_tool_events(), openai_text_events("391")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as http_client:
        client = AsyncOpenAI(
            api_key="test",
            base_url="https://openai.test/v1",
            http_client=http_client,
            max_retries=0,
        )
        provider = OpenAIResponsesProvider(client=client)
        async with build(
            settings=memory_settings(),
            storage="memory",
            model_policy="balanced",
            model_provider_overrides={"openai": provider},
            enabled_tools=["math.calculate"],
        ) as composition:
            run_id = await composition.runs.submit("Calculate 17 * 23 with reasoning.")
            run = await composition.runs.get(run_id)

    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "391"
    assert len(wire.requests) == 2
    first, second = wire.requests
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
        "summary": [],
    }
    assert function_call["call_id"] == "golden-openai-tool-call"
    assert function_call["arguments"] == '{"expression":"17 * 23"}'
    assert function_result["call_id"] == "golden-openai-tool-call"
    assert "391" in function_result["output"]
    assert replay.index(reasoning) < replay.index(function_call) < replay.index(function_result)


async def test_approval_parks_resolves_and_resumes_through_the_public_api() -> None:
    """The full approval loop at the HTTP boundary.

    A scripted external write parks the run; the approval is listed, read,
    and approved over the API; a separate worker composition resumes the
    re-dispatched run to completion; the replayed stream shows the request,
    the resolution, the effect, and the terminal event in order.
    """
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "api-approved"},
                        call_id="golden-approval",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="External write recorded.", stop_reason=StopReason.END_TURN),
        ]
    )
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
            headers={"Idempotency-Key": "golden-approval"},
            json={"content": [{"type": "text", "text": "record an external write"}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = UUID(submitted.json()["run_id"])

        await _run_worker(worker_composition, "golden-approval-parker")
        parked = await client.get(f"/v1/runs/{run_id}")
        assert parked.status_code == 200, parked.text
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value

        listing = await client.get(f"/v1/approvals?run_id={run_id}")
        assert listing.status_code == 200, listing.text
        items = listing.json()["items"]
        assert len(items) == 1
        approval = items[0]
        assert approval["run_id"] == str(run_id)
        assert approval["status"] == "PENDING"
        assert approval["tool_name"] == "demo.external_write"
        assert approval["arguments"]["content"] == "api-approved"
        assert approval["risk"]
        assert approval["policy_reason"]

        resolved = await client.post(
            f"/v1/approvals/{approval['id']}/resolve",
            json={"decision": "approve_once"},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["decision"] == "approve_once"
        assert resolved.json()["resolved_at"] is not None

        await _run_worker(worker_composition, "golden-approval-resumer")
        completed = await client.get(f"/v1/runs/{run_id}")
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == RunStatus.COMPLETED.value
        assert completed.json()["tool_call_count"] == 1

        stream = await client.get(f"/v1/runs/{run_id}/events", timeout=10.0)

    assert stream.status_code == 200, stream.text
    frames = _sse_frames(stream.text)
    terminal = next(frame for frame in frames if frame["event"] == "run.completed")
    assert terminal["data"]["final_message"]["content"] == [
        {"kind": "text", "text": "External write recorded."}
    ]
    ordered = [frame["event"] for frame in frames]
    for earlier, later in [
        ("approval.requested", "approval.resolved"),
        ("approval.resolved", "tool.call.completed"),
        ("tool.call.completed", "assistant.message.completed"),
        ("assistant.message.completed", "run.completed"),
    ]:
        assert ordered.index(earlier) < ordered.index(later), ordered


async def test_created_schedule_fires_and_its_run_executes_to_completion() -> None:
    """A schedule created over the API produces a run that actually executes.

    The materializer and the durable worker are elsewhere proven separately;
    this journey chains them: POST /v1/schedules, advance the clock to the
    fire instant, one schedule-worker pass materializes the occurrence, one
    durable-worker pass executes the run, and the occurrence history links
    the completed run over HTTP.
    """

    now = datetime(2026, 8, 25, 16, tzinfo=UTC)
    agent_id = UUID("00000000-0000-0000-0000-000000000619")
    script = FakeModelScript(
        turns=[ScriptedTurn(text="Scheduled briefing done.", stop_reason=StopReason.END_TURN)]
    )
    settings = dataclasses_replace(
        database_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
    )
    async with (
        build(
            settings=settings,
            storage="postgres",
            script=script,
            fixed_clock_at=now,
        ) as composition,
        _client(composition) as client,
    ):
        async with composition.uow_factory() as uow:
            await uow.agents.put(
                AgentSpec(
                    id=agent_id,
                    version="1.0.0",
                    name="Golden schedule agent",
                    instructions="Follow the scheduled instruction.",
                    model_policy="fake-balanced",
                    enabled_tools=[],
                    policy_profile="default",
                    limits=RunLimits(),
                )
            )
        created = await client.post(
            "/v1/schedules",
            headers={"Idempotency-Key": "golden-schedule"},
            json={
                "title": "Golden daily briefing",
                "instruction": "Summarize project changes.",
                "agent_id": str(agent_id),
                "agent_version": "1.0.0",
                "policy_profile": "default",
                "requested_scopes": ["workspace.read"],
                "limits": {
                    "max_steps": 4,
                    "max_model_calls": 4,
                    "max_tool_calls": 4,
                    "max_cost": str(Decimal("1")),
                },
                "run_timeout_seconds": 3600,
                "cadence": {
                    "kind": "DAILY",
                    "local_time": civil_time(9).isoformat(),
                    "timezone": "America/Los_Angeles",
                },
                "misfire_grace_seconds": 60,
                "max_consecutive_failures": 3,
            },
        )
        assert created.status_code == 201, created.text
        schedule_id = UUID(created.json()["schedule"]["id"])

        record = await composition.schedules.get(composition.principal, schedule_id)
        assert record.schedule.next_fire_at is not None
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(record.schedule.next_fire_at - clock.now())

        schedule_worker = composition.schedule_worker_factory()
        assert isinstance(schedule_worker, ScheduleWorker)
        assert await schedule_worker.run_once() == 1
        await _run_worker(composition, "golden-schedule-runner")

        occurrences = await client.get(f"/v1/schedules/{schedule_id}/occurrences")
        assert occurrences.status_code == 200, occurrences.text
        rows = occurrences.json()["items"]
        assert len(rows) == 1
        run_id = rows[0]["run_id"]
        assert run_id is not None

        run = await client.get(f"/v1/runs/{run_id}")
        assert run.status_code == 200, run.text
        assert run.json()["status"] == RunStatus.COMPLETED.value

        stream = await client.get(f"/v1/runs/{run_id}/events", timeout=10.0)

    assert stream.status_code == 200, stream.text
    frames = _sse_frames(stream.text)
    terminal = next(frame for frame in frames if frame["event"] == "run.completed")
    assert terminal["data"]["final_message"]["content"] == [
        {"kind": "text", "text": "Scheduled briefing done."}
    ]


async def test_parked_approval_notifies_a_registered_device_through_the_outbox() -> None:
    """The notification pipeline joined end to end.

    A device registers over the API, a scripted external write parks the run,
    the producer's outbox row is dispatched to the fake push transport with a
    content-free payload, and the offline inbox reports the delivery.
    """

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "sensitive-effect-body"},
                        call_id="golden-notify",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    settings = dataclasses_replace(
        database_settings(),
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    async with (
        build(settings=settings, storage="postgres", script=script) as composition,
        _client(composition) as client,
    ):
        registered = await client.post(
            "/v1/devices",
            headers={"Idempotency-Key": "golden-device"},
            json={
                "client_device_id": "golden-phone",
                "name": "Golden iPhone",
                "kind": "mobile",
                "platform": "ios",
                "app_bundle_id": "com.veetbot.app",
                "push_provider": "apns",
                "push_token": "0123456789abcdef",
                "push_environment": "sandbox",
                "muted_kinds": [],
            },
        )
        assert registered.status_code == 201, registered.text

        created = await client.post(
            "/v1/sessions",
            json={"agent_id": "general", "metadata": {}},
        )
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"Idempotency-Key": "golden-notify"},
            json={"content": [{"type": "text", "text": "record an external write"}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = UUID(submitted.json()["run_id"])
        await _run_worker(composition, "golden-notify-parker")
        parked = await client.get(f"/v1/runs/{run_id}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value

        transport = FakePushTransport()
        dispatcher = NotificationDispatcher(
            uow_factory=composition.uow_factory,
            transport=transport,
            providers=frozenset({PushProvider.APNS}),
            clock=composition.clock,
            ids=composition.ids,
            claimant="golden-dispatcher",
            batch_size=10,
            lease_seconds=30,
            retry_delays=(30, 120, 600, 3600),
        )
        assert await dispatcher.run_once() == 1
        assert await dispatcher.run_once() == 0

        inbox = await client.get("/v1/notifications")

    assert len(transport.calls) == 1
    target, message = transport.calls[0]
    assert target.token.get_secret_value() == "0123456789abcdef"
    payload = message.payload.model_dump(mode="json")
    assert payload["kind"] == "approval_requested"
    flattened = json.dumps(payload)
    assert "sensitive-effect-body" not in flattened
    assert "record an external write" not in flattened

    assert inbox.status_code == 200, inbox.text
    items = inbox.json()["items"]
    assert len(items) == 1
    assert items[0]["notification"]["kind"] == "approval_requested"
    assert items[0]["notification"]["run_id"] == str(run_id)
    outcomes = [delivery["outcome"] for delivery in items[0]["deliveries"]]
    assert outcomes == ["delivered"]


async def test_implicitly_formed_memory_is_browsable_through_the_read_api() -> None:
    """Formation, consolidation, and the Milestone 17 read surface in one chain.

    A run's user message implicitly yields a belief through idle
    consolidation — no memory.remember call anywhere — and the belief a real
    agent run formed is then listed and opened over GET /v1/memories with its
    provenance pointing back at the forming session and run.
    """

    now = datetime(2026, 8, 25, 16, tzinfo=UTC)
    clock = FixedClock(now)
    settings = dataclasses_replace(database_settings(), memory_api_enabled=True)
    script = FakeModelScript(turns=[ScriptedTurn(text="Thanks for telling me.")])
    async with (
        build(settings=settings, storage="postgres", script=script, clock=clock) as composition,
        _client(composition) as client,
    ):
        run_id = await composition.runs.submit("I have an Apple Watch and a BMW X3.")
        await _run_worker(composition, "golden-memory-former")
        run = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, composition.principal)
        assert "memory.remember" not in _tool_names(events, "tool.call.completed")
        formation_event = next(
            event for event in events if event.event_type == "memory.formation.requested"
        )
        not_before = datetime.fromisoformat(str(formation_event.payload["not_before"]))
        clock.advance(not_before - clock.now())
        maintenance = cast(MaintenanceWorker, composition.maintenance_factory())
        await maintenance.run_once()

        listing = await client.get("/v1/memories", params={"ceiling": "restricted"})
        assert listing.status_code == 200, listing.text
        items = listing.json()["items"]
        assert items, "idle consolidation formed no browsable belief"
        formed = [item for item in items if str(run.session_id) == item["source_session_id"]]
        assert formed, items
        watch = next(
            (item for item in formed if "Apple Watch" in item["statement"]),
            None,
        )
        assert watch is not None, [item["statement"] for item in formed]

        detail = await client.get(f"/v1/memories/{watch['id']}", params={"ceiling": "restricted"})
        assert detail.status_code == 200, detail.text
        assert detail.json()["statement"] == watch["statement"]
        assert detail.json()["source_session_id"] == str(run.session_id)
        assert detail.json()["formation_run_id"]
        assert detail.json()["source_event_ids"]


async def test_run_produced_artifact_downloads_through_the_public_api(tmp_path: Path) -> None:
    """A run writes a workspace file, exports it, and the bytes come back
    over GET /v1/artifacts/{id}/content as an attachment.

    Every earlier HTTP artifact test injected the artifact row by hand; this
    journey earns it through the scripted tool pipeline instead.
    """

    content = "the golden artifact body\n"
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="workspace.write_text",
                        arguments={"path": "output/report.txt", "content": content},
                        call_id="golden-artifact-write",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="artifact.export",
                        arguments={
                            "path": "output/report.txt",
                            "filename": "report.txt",
                            "media_type": "text/plain",
                        },
                        call_id="golden-artifact-export",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Report exported.", stop_reason=StopReason.END_TURN),
        ]
    )
    settings = dataclasses_replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with (
        build(settings=settings, storage="postgres", script=script) as composition,
        _client(composition) as client,
    ):
        created = await client.post(
            "/v1/sessions",
            json={"agent_id": "general", "metadata": {}},
        )
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"Idempotency-Key": "golden-artifact"},
            json={"content": [{"type": "text", "text": "export the report"}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = UUID(submitted.json()["run_id"])

        await _run_worker(composition, "golden-artifact-worker")
        for attempt in range(3):
            run = await client.get(f"/v1/runs/{run_id}")
            if run.json()["status"] != RunStatus.WAITING_FOR_APPROVAL.value:
                break
            approval = (await composition.approvals.list_pending(run_id=run_id))[0]
            await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
            await _run_worker(composition, f"golden-artifact-worker-{attempt}")
        run = await client.get(f"/v1/runs/{run_id}")
        assert run.json()["status"] == RunStatus.COMPLETED.value

        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
        export = next(
            invocation for invocation in invocations if invocation.tool_name == "artifact.export"
        )
        assert export.structured_result is not None
        artifact_id = str(export.structured_result["artifact_id"])

        metadata = await client.get(f"/v1/artifacts/{artifact_id}")
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["name"] == "report.txt"
        assert metadata.json()["media_type"] == "text/plain"
        assert metadata.json()["run_id"] == str(run_id)

        download = await client.get(f"/v1/artifacts/{artifact_id}/content")

    assert download.status_code == 200, download.text
    assert download.text == content
    assert download.headers["content-disposition"].startswith("attachment")
