"""Golden user journeys spanning the runtime, tools, provider wire, and public API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime, timedelta
from datetime import time as civil_time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
from openai import AsyncOpenAI

from agent_core.adapters.determinism import FixedClock, SystemClock
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.push import FakePushTransport
from agent_core.bootstrap import Composition, build, build_notification_worker
from agent_core.config import (
    AuthMode,
    DeploymentMode,
    PushProviderKind,
    SandboxMechanism,
    Settings,
)
from agent_core.domain.agents import AgentSpec
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefType
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
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


async def _run_worker_on_wall_clock(composition: Composition, worker_id: str) -> None:
    """Run one claim whose heartbeat waits use real time.

    FixedClock.sleep returns at once, so heartbeats on the domain clock can
    exhaust a tool's deadline during database I/O; domain time stays fixed.
    """

    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=SystemClock(),
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
        # ADR-0122: the exported file rides on the reply the owner sees.
        transcript = await client.get(f"/v1/sessions/{session_id}/messages")
        async with composition.uow_factory() as uow:
            stored = await uow.artifacts.get(UUID(artifact_id), composition.principal)
            events = [
                event
                for event in await uow.events.list_after(session_id, 0, composition.principal)
                if event.run_id == run_id
            ]

    assert download.status_code == 200, download.text
    assert download.text == content
    assert download.headers["content-disposition"].startswith("attachment")
    assert transcript.status_code == 200, transcript.text
    assert transcript.json()["items"][-1]["content"] == [
        {"type": "text", "text": "Report exported."},
        {
            "type": "file",
            "artifact_id": artifact_id,
            "media_type": "text/plain",
            "filename": "report.txt",
        },
    ]
    assert stored.expires_at is None
    [reply] = [
        event.payload["message"]
        for event in events
        if event.event_type == "assistant.message.completed"
    ]
    [completed] = [event.payload for event in events if event.event_type == "run.completed"]
    assert completed["final_message"] == reply


def _notification_role_settings(tmp_path: Path) -> Settings:
    """The lean production notification process: its own role, no app credentials."""

    apns_key = tmp_path / "AuthKey_GOLDEN.p8"
    apns_key.write_text("golden APNs private key material", encoding="ascii")
    apns_key.chmod(0o600)
    return dataclasses_replace(
        database_settings(),
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        sandbox=SandboxMechanism.GVISOR,
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"notify"}),
        auth_scopes=PLATFORM_SCOPES,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
        push_provider=PushProviderKind.APNS,
        apns_key_file=apns_key,
        apns_key_id="KEYID",
        apns_team_id="TEAMID",
        apns_topic="com.veetbot.app",
    )


async def test_a_schedule_made_in_chat_runs_while_away_and_its_result_reaches_the_phone(
    tmp_path: Path,
) -> None:
    """The away-from-keyboard loop, from a chat message to the lock screen.

    The owner asks Chat for a reminder and approves the proposed schedule over
    HTTP. At the fire instant the schedule worker materializes the occurrence,
    a durable worker runs it, and accounting records the outcome and enqueues
    `schedule_run_finished` in the same transaction. The separate production
    notification process delivers both alerts to the device registered over
    HTTP: the approval alert while the run waits, the result once it finishes,
    each after the thirty-second attention grace period (ADR-0143).
    The pinned title may ride along (ADR-0091); the instruction and the
    scheduled reply may not.
    """

    now = datetime(2026, 8, 25, 20, tzinfo=UTC)
    fire_at = datetime(2026, 8, 26, 2, tzinfo=UTC)
    request_text = "Remind me at 7pm to stretch my back."
    title = "Stretch break"
    instruction = "Remind me to stand up and stretch my back."
    scheduled_reply = "Time to stand up and stretch your back."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create",
                        arguments={
                            "title": title,
                            "instruction": instruction,
                            "at": fire_at.isoformat(),
                        },
                        call_id="golden-chat-schedule",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I scheduled your stretch reminder."),
            ScriptedTurn(text=scheduled_reply),
        ]
    )
    settings = dataclasses_replace(
        database_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    transport = FakePushTransport()
    clock = FixedClock(now)
    async with (
        build(settings=settings, storage="postgres", script=script, clock=clock) as composition,
        build_notification_worker(
            settings=_notification_role_settings(tmp_path), transport=transport, clock=clock
        ) as notifier,
        _client(composition) as client,
    ):
        registered = await client.post(
            "/v1/devices",
            headers={"Idempotency-Key": "golden-away-device"},
            json={
                "client_device_id": "golden-away-phone",
                "name": "Golden iPhone",
                "kind": "mobile",
                "platform": "ios",
                "app_bundle_id": "com.veetbot.app",
                "push_provider": "apns",
                "push_token": "fedcba9876543210",
                "push_environment": "sandbox",
                "muted_kinds": [],
            },
        )
        assert registered.status_code == 201, registered.text
        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"Idempotency-Key": "golden-away-chat"},
            json={"content": [{"type": "text", "text": request_text}]},
        )
        assert submitted.status_code == 202, submitted.text
        chat_run_id = UUID(submitted.json()["run_id"])

        await _run_worker_on_wall_clock(composition, "golden-away-proposer")
        assert await notifier.run_once() == 0
        clock.advance(timedelta(seconds=29))
        assert await notifier.run_once() == 0
        clock.advance(timedelta(seconds=1))
        assert await notifier.run_once() == 1
        [approval] = (
            await client.get("/v1/approvals", params={"run_id": str(chat_run_id)})
        ).json()["items"]
        assert approval["tool_name"] == "schedule.create"
        resolved = await client.post(
            f"/v1/approvals/{approval['id']}/resolve", json={"decision": "approve_once"}
        )
        assert resolved.status_code == 200, resolved.text
        await _run_worker_on_wall_clock(composition, "golden-away-creator")
        chat_run = await client.get(f"/v1/runs/{chat_run_id}")
        assert chat_run.json()["status"] == RunStatus.COMPLETED.value, chat_run.json()

        listed = await client.get("/v1/schedules")
        assert listed.status_code == 200, listed.text
        [schedule] = listed.json()["items"]
        assert schedule["title"] == title
        assert datetime.fromisoformat(schedule["next_fire_at"]) == fire_at
        schedule_id = UUID(schedule["id"])
        clock.advance(fire_at - clock.now())
        schedule_worker = composition.schedule_worker_factory()
        assert isinstance(schedule_worker, ScheduleWorker)
        assert await schedule_worker.run_once() == 1
        await _run_worker_on_wall_clock(composition, "golden-away-occurrence")

        [occurrence] = (await client.get(f"/v1/schedules/{schedule_id}/occurrences")).json()[
            "items"
        ]
        scheduled_run = await client.get(f"/v1/runs/{occurrence['run_id']}")
        assert scheduled_run.json()["status"] == RunStatus.COMPLETED.value, scheduled_run.json()
        async with composition.uow_factory() as uow:
            [accounted] = await uow.process_events.list("schedule.run_accounted")
        assert accounted.payload["run_id"] == occurrence["run_id"]
        assert accounted.payload["run_status"] == RunStatus.COMPLETED.value

        assert await notifier.run_once() == 0
        clock.advance(timedelta(seconds=29))
        assert await notifier.run_once() == 0
        clock.advance(timedelta(seconds=1))
        assert await notifier.run_once() == 1
        assert await notifier.run_once() == 0
        inbox = await client.get("/v1/notifications")

    payloads = [message.payload.model_dump(mode="json") for _target, message in transport.calls]
    assert {target.token.get_secret_value() for target, _message in transport.calls} == {
        "fedcba9876543210"
    }
    assert [payload["kind"] for payload in payloads] == [
        "approval_requested",
        "schedule_run_finished",
    ]
    finished = payloads[1]
    assert finished["schedule_id"] == str(schedule_id)
    assert finished["run_id"] == occurrence["run_id"]
    assert finished["status"] == RunStatus.COMPLETED.value
    assert finished["schedule_context"]["title"] == title
    assert datetime.fromisoformat(finished["schedule_context"]["scheduled_for"]) == fire_at
    requested = payloads[0]
    assert requested["run_id"] == str(chat_run_id)
    assert requested["approval_id"] == approval["id"]
    # Alerts are content-free: no request, tool argument or reply reaches Apple.
    flattened = json.dumps(payloads)
    for private in (request_text, instruction, scheduled_reply):
        assert private not in flattened

    assert inbox.status_code == 200, inbox.text
    delivered = {
        item["notification"]["kind"]: [delivery["outcome"] for delivery in item["deliveries"]]
        for item in inbox.json()["items"]
    }
    assert delivered == {
        "approval_requested": ["delivered"],
        "schedule_run_finished": ["delivered"],
    }


async def test_a_fact_formed_in_one_chat_informs_the_first_answer_of_the_next() -> None:
    """Implicit formation joined to recall, across two chats and two processes.

    The first chat never calls memory.remember: idle consolidation forms the
    belief from the owner's own message. A later process opens a new chat, and
    its very first model request must already carry that belief, with no memory
    tool call, and the answer is delivered over the API.
    """

    now = datetime(2026, 8, 25, 16, tzinfo=UTC)
    clock = FixedClock(now)
    remembered = "BMW X3"
    first_script = FakeModelScript(turns=[ScriptedTurn(text="Noted, thanks.")])
    async with build(
        settings=database_settings(), storage="postgres", script=first_script, clock=clock
    ) as first:
        first_run_id = await first.runs.submit("I drive a BMW X3 to work every day.")
        await _run_worker(first, "golden-formation-chat")
        first_run = await first.runs.get(first_run_id)
        async with first.uow_factory() as uow:
            first_events = await uow.events.list_after(first_run.session_id, 0, first.principal)
        formation = next(
            event for event in first_events if event.event_type == "memory.formation.requested"
        )
        clock.advance(datetime.fromisoformat(str(formation.payload["not_before"])) - clock.now())
        await cast(MaintenanceWorker, first.maintenance_factory()).run_once()
        formed = [
            memory.statement
            for memory in await first.memory.list_memories()
            if remembered in memory.statement
        ]
    assert first_run.status is RunStatus.COMPLETED
    assert formed, "idle consolidation formed no belief about the car"

    second_script = FakeModelScript(
        turns=[ScriptedTurn(text="You drive a BMW X3.", context_contains=remembered)]
    )
    async with (
        build(settings=database_settings(), storage="postgres", clock=clock) as api,
        build(
            settings=database_settings(), storage="postgres", script=second_script, clock=clock
        ) as worker,
        _client(api) as client,
    ):
        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"Idempotency-Key": "golden-recall-next-chat"},
            json={"content": [{"type": "text", "text": "What car do I drive?"}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = UUID(submitted.json()["run_id"])
        await _run_worker(worker, "golden-recall-next-chat")
        run = await client.get(f"/v1/runs/{run_id}")
        transcript = await client.get(f"/v1/sessions/{session_id}/messages")
        events = await worker.runs.events(run_id)

    assert run.json()["status"] == RunStatus.COMPLETED.value, run.json()
    assert transcript.json()["items"][-1]["content"] == [
        {"type": "text", "text": "You drive a BMW X3."}
    ]
    assert [event.event_type for event in events].count("model.request.started") == 1
    assert _tool_names(events, "tool.call.completed") == []
    assert "memory.recalled" in {event.event_type for event in events}


async def test_a_file_dropped_into_chat_reaches_the_provider_and_answers_a_later_chat(
    tmp_path: Path,
) -> None:
    """ADR-0120 end to end: upload, claim on send, release to the model, ingest, recall.

    The owner uploads an image and a Markdown note over HTTP and sends them with
    a message. A durable worker runs the turn through the OpenAI SDK request path,
    and the serialized request carries both files' bytes. The maintenance worker
    adds the owner-sent note to knowledge, and a later chat answers from it
    through knowledge.search on PostgreSQL.
    """

    import base64

    from tests.gates.test_attachment_upload_adr0120 import PNG

    note = b"# Garden plan\n\nWater the tomatoes every morning before nine."
    settings = dataclasses_replace(
        database_settings(),
        artifact_root=tmp_path / "artifacts",
        attachment_uploads_enabled=True,
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
    )
    wire = _OpenAIWire(
        [
            openai_text_events("I have your garden plan and the photo."),
            # ADR-0155: the maintenance round titles the conversation.
            openai_text_events('{"decision": "replace", "title": "Garden plan and photo"}'),
        ]
    )
    async with (
        # A sibling process of the same configuration lends the provider its
        # attachment resolver; production composes each provider with its own.
        build(settings=settings, storage="postgres") as resolver_host,
        httpx.AsyncClient(transport=httpx.MockTransport(wire)) as http_client,
    ):
        provider = OpenAIResponsesProvider(
            client=AsyncOpenAI(
                api_key="test",
                base_url="https://openai.test/v1",
                http_client=http_client,
                max_retries=0,
            ),
            attachment_resolver=resolver_host.attachment_resolver,
        )
        async with (
            build(
                settings=settings,
                storage="postgres",
                model_policy="balanced",
                model_provider_overrides={"openai": provider},
            ) as composition,
            _client(composition) as client,
        ):
            created = await client.post(
                "/v1/sessions", json={"agent_id": "general", "metadata": {}}
            )
            assert created.status_code == 201, created.text
            session_id = UUID(created.json()["id"])

            async def upload(content: bytes, name: str, media_type: str) -> str:
                response = await client.post(
                    f"/v1/sessions/{session_id}/artifacts",
                    content=content,
                    headers={
                        "Content-Type": media_type,
                        "X-Filename": name,
                        "Idempotency-Key": f"golden-upload-{name}",
                    },
                )
                assert response.status_code == 201, response.text
                return str(response.json()["id"])

            image_id = await upload(PNG, "tomatoes.png", "image/png")
            note_id = await upload(note, "garden.md", "text/markdown")
            sent = await client.post(
                f"/v1/sessions/{session_id}/messages",
                headers={"Idempotency-Key": "golden-attachment-send"},
                json={
                    "content": [
                        {"type": "text", "text": "Here is my garden plan and a photo."},
                        {"type": "image", "artifact_id": image_id, "media_type": "image/png"},
                        {"type": "file", "artifact_id": note_id, "media_type": "text/markdown"},
                    ]
                },
            )
            assert sent.status_code == 202, sent.text
            run_id = UUID(sent.json()["run_id"])
            await _run_worker(composition, "golden-attachment-worker")
            run = await client.get(f"/v1/runs/{run_id}")
            assert run.json()["status"] == RunStatus.COMPLETED.value, run.json()

            await cast(MaintenanceWorker, composition.maintenance_factory()).run_once()
            async with composition.uow_factory() as uow:
                ingested = await uow.artifacts.get(UUID(note_id), composition.principal)
                titled = await uow.sessions.get(session_id, composition.principal)
            assert ingested.metadata["auto_ingest"] == "ingested", ingested.metadata
            assert titled.title == "Garden plan and photo"

    # The provider received the image bytes and the note's text, fenced as data.
    # The maintenance round's title call follows it (ADR-0155): it reads only
    # the owner's words, so neither file's contents reach it.
    request, title_request = wire.requests
    title_wire = json.dumps(title_request)
    assert "Title a chat conversation" in title_wire
    assert base64.b64encode(PNG).decode() not in title_wire
    assert "Water the tomatoes" not in title_wire
    parts = request["input"][-1]["content"]
    image_url = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    assert [part["image_url"] for part in parts if part["type"] == "input_image"] == [image_url]
    [note_part] = [
        part["text"]
        for part in parts
        if part["type"] == "input_text" and part["text"].startswith('[Attached file "garden.md"')
    ]
    assert note.decode() in note_part
    assert '<untrusted trust="external_untrusted"' in note_part

    passage = "Water the tomatoes every morning before nine."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="knowledge.search",
                        arguments={"text": "when to water the tomatoes"},
                        call_id="golden-knowledge-search",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Every morning before nine.", context_contains=passage),
        ]
    )
    async with build(settings=settings, storage="postgres", script=script) as later:
        later_session = await later.sessions.create()
        later_run_id = await later.runs.submit("When should I water the tomatoes?", later_session)
        await _run_worker(later, "golden-knowledge-worker")
        later_run = await later.runs.get(later_run_id)
        later_events = await later.runs.events(later_run_id)

    assert later_run.status is RunStatus.COMPLETED, later_run.failure
    assert later_run.final_message == "Every morning before nine."
    assert _tool_names(later_events, "tool.call.completed") == ["knowledge.search"]
