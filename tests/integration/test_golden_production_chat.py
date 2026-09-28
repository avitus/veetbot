"""Golden journey: a production-shaped Chat carried across process restarts.

Every earlier proof of Chat's production roster (ADR-0123, ADR-0124) ran on the
in-memory store inside one process, and every earlier PostgreSQL chat used a
reduced roster. The regressions that reached production lived in the gap: a
stored plan that read back differently from jsonb failed every new chat, and
scope-set order re-planned chats after a restart (ADR-0134). A reasoning
provider's continuation was unreachable in tests until the fake could script
one, and a resume that mishandled it killed a production run.

This journey joins those halves. The owner talks to Chat over HTTP with every
roster-changing production flag on: People, Email mode on two accounts, email
unsubscribe, scheduling, web and calling. Each model step runs in a fresh
worker composition, as after a deploy, reading the plan, checkpoint and
provider continuation back from PostgreSQL. The model asks two clarifying
questions, then starts a call through the deferred tool index; the owner
approves over HTTP and the call is dispatched once.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
from pydantic import SecretStr

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings, load_settings
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ModelRequest,
    ProviderReasoningItem,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    ToolCallItem,
)
from agent_core.domain.runs import RunStatus
from agent_core.mcp.configuration import email_server_configs
from agent_core.runtime.worker import DurableWorker
from bland_mcp.client import BlandClient
from bland_mcp.server import create_server as create_bland_server
from tests.contract.test_bland_client_contract import CALL_ID, KEY, NUMBER
from tests.gates.test_call_lifecycle import arguments as call_arguments
from tests.gates.test_call_m27 import call_configuration
from tests.gates.test_email_m18 import (
    _accounts_manifest,
    _base_environment,
    _generated_gmail_discovery,
)
from tests.gates.test_email_unsubscribe_m31 import Transport
from tests.integration.disposable_database import disposable_database_url
from tests.unit.test_web_tools import FakeWebProvider

START_CALL = "mcp.bland_call.start_call"
CALL_READS = frozenset({"mcp.bland_read.list_calls", "mcp.bland_read.get_call"})
CHAT_TOOL_CAP = 30


async def _bland_discovery(mode: str) -> MCPDiscovery:
    catalog = create_bland_server(mode, BlandClient(KEY, NUMBER), call_configuration().model_dump())
    return MCPDiscovery(
        tools=tuple(
            MCPRemoteTool(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
                application_only=(tool.meta or {}).get("veetbot/application-only") is True,
            )
            for tool in await catalog.list_tools()
        )
    )


def _production_settings(tmp_path: Path) -> Settings:
    """Every flag production enables that changes Chat's default roster."""

    configuration = call_configuration()
    loaded = load_settings(
        {
            **_base_environment(),
            "AGENT_EMAIL_ENABLED": "1",
            "AGENT_EMAIL_MODE_ENABLED": "1",
            "GMAIL_ACCOUNTS_FILE": str(_accounts_manifest(tmp_path)),
            # ADR-0140: the TensorScale key adds image.generate and video.generate.
            "TENSORSCALE_API_KEY": "synthetic-media-credential",
        }
    )
    assert loaded.people_enabled
    assert "tensorscale" in loaded.credentials
    return replace(
        loaded,
        database_url=disposable_database_url(),
        email_unsubscribe_enabled=True,
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
        call_enabled=True,
        call_configuration=configuration,
        credentials={
            **loaded.credentials,
            **{
                name: SecretStr(
                    json.dumps({"api_key": KEY, "configuration": configuration.model_dump()})
                )
                for name in ("bland_read", "bland_call")
            },
        },
    )


async def _mcp_factory(settings: Settings) -> ScriptedMCPClientFactory:
    scripts = {
        row.server_id: ScriptedMCPServer(
            name=row.server_id,
            discovery=await _generated_gmail_discovery(row.server_id.rsplit("_", 1)[-1]),
        )
        for row in email_server_configs("local", account_ids=settings.email_account_ids)
    }
    scripts["bland_read"] = ScriptedMCPServer(
        name="bland_read", discovery=await _bland_discovery("read")
    )
    scripts["bland_call"] = ScriptedMCPServer(
        name="bland_call",
        discovery=await _bland_discovery("call"),
        responses=(
            ScriptedMCPResponse(
                name="start_call",
                result=MCPCallResult(
                    structured={"provider_call_id": CALL_ID, "status": "accepted"}
                ),
            ),
        ),
    )
    return ScriptedMCPClientFactory(scripts)


@asynccontextmanager
async def _process(
    settings: Settings, turns: Sequence[ScriptedTurn] = ()
) -> AsyncIterator[tuple[Composition, ScriptedMCPClientFactory, FakeModelProvider]]:
    """One API or worker process of the production topology."""

    factory = await _mcp_factory(settings)
    web = FakeWebProvider()
    async with build(
        settings=settings,
        storage="postgres",
        script=FakeModelScript(turns=list(turns)),
        mcp_client_factory=factory,
        web_search_provider_override=web,
        web_fetch_provider_override=web,
        one_click_transport_override=Transport(),
    ) as composition:
        model = composition.executor._model_provider
        assert isinstance(model, FakeModelProvider)
        yield composition, factory, model


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(
        app=app, client=("127.0.0.1", 43106), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _restarted_worker_step(
    settings: Settings, turn: ScriptedTurn, worker_id: str
) -> tuple[int, list[ModelRequest]]:
    """Run one claim in a fresh worker process scripted with exactly one turn.

    Returns the MCP tool calls the process made and the model requests it sent.
    """

    async with _process(settings, [turn]) as (composition, factory, model):
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id=worker_id,
        )
        assert await worker.run_once()
        return sum(client.call_count for client in factory.created), list(model.requests)


def _ask(question: str, call_id: str, reasoning_id: str) -> ScriptedTurn:
    return ScriptedTurn(
        provider_reasoning_payload={"id": reasoning_id, "summary": []},
        tool_calls=[
            ScriptedToolCall(
                name="conversation.ask_user",
                arguments={"question": question},
                call_id=call_id,
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def _sse_frames(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            frames.append((fields["event"], json.loads(fields.get("data", "{}"))))
    return frames


def _assert_continuation_replayed(
    requests: Sequence[ModelRequest], reasoning_id: str, call_id: str
) -> None:
    """The restarted process's first request replays the opaque reasoning block
    the provider returned with the call, immediately before that call."""

    assert requests, "the restarted worker sent no model request"
    items = list(requests[0].conversation)
    replayed = [
        index
        for index, item in enumerate(items[:-1])
        if isinstance(item, ProviderReasoningItem)
        and item.provider_payload.get("id") == reasoning_id
        and isinstance(items[index + 1], ToolCallItem)
        and cast(ToolCallItem, items[index + 1]).call_id == call_id
    ]
    assert len(replayed) == 1, [
        (item.kind, getattr(item, "call_id", None)) for item in items if item.kind != "system"
    ]


async def test_production_chat_survives_restarts_through_clarifications_approval_and_a_call(
    tmp_path: Path,
) -> None:
    settings = _production_settings(tmp_path)
    async with _process(settings) as (api, _factory, _model), _client(api) as client:
        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"Idempotency-Key": "golden-production-chat"},
            json={"content": [{"type": "text", "text": "Call the dentist about ticket 12."}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = UUID(submitted.json()["run_id"])

        async def answer(text: str) -> None:
            run = await client.get(f"/v1/runs/{run_id}")
            assert run.json()["status"] == RunStatus.WAITING_FOR_USER.value, run.json()
            events = await api.runs.events(run_id)
            question = next(
                event for event in reversed(events) if event.event_type == "run.waiting_for_user"
            )
            delivered = await client.post(
                f"/v1/runs/{run_id}/input",
                json={
                    "content": [{"type": "text", "text": text}],
                    "question_id": question.payload["question_id"],
                },
            )
            assert delivered.status_code in {200, 202}, delivered.text

        dispatched, _requests = await _restarted_worker_step(
            settings,
            _ask("Which dentist, and may I share the ticket number?", "clarify-1", "rs_ask_1"),
            "production-chat-worker-1",
        )
        assert dispatched == 0
        first_plan = await api.executor._context_planner.current(session_id)
        await answer("Dr. Lee. Yes, share ticket 12.")

        dispatched, requests = await _restarted_worker_step(
            settings,
            _ask("Should the call be recorded?", "clarify-2", "rs_ask_2"),
            "production-chat-worker-2",
        )
        assert dispatched == 0
        _assert_continuation_replayed(requests, "rs_ask_1", "clarify-1")
        await answer("No recording.")

        dispatched, requests = await _restarted_worker_step(
            settings,
            ScriptedTurn(
                provider_reasoning_payload={"id": "rs_call", "summary": []},
                tool_calls=[
                    ScriptedToolCall(
                        name="tool.call",
                        arguments={"name": START_CALL, "arguments": call_arguments()},
                        call_id="deferred-start-call",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            "production-chat-worker-3",
        )
        assert dispatched == 0, "the call was dispatched before the owner approved it"
        _assert_continuation_replayed(requests, "rs_ask_2", "clarify-2")
        parked = await client.get(f"/v1/runs/{run_id}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.json()
        listing = await client.get("/v1/approvals", params={"run_id": str(run_id)})
        assert listing.status_code == 200, listing.text
        [approval] = listing.json()["items"]
        assert approval["tool_name"] == START_CALL
        resolved = await client.post(
            f"/v1/approvals/{approval['id']}/resolve", json={"decision": "approve_once"}
        )
        assert resolved.status_code == 200, resolved.text

        dispatched, requests = await _restarted_worker_step(
            settings,
            ScriptedTurn(text="I called Dr. Lee's office about ticket 12."),
            "production-chat-worker-4",
        )
        _assert_continuation_replayed(requests, "rs_call", "deferred-start-call")

        completed = await client.get(f"/v1/runs/{run_id}")
        stream = await client.get(f"/v1/runs/{run_id}/events", timeout=10.0)
        final_plan = await api.executor._context_planner.current(session_id)
        async with api.uow_factory() as uow:
            reservations = await uow.calls.list(api.principal, "dispatch")
            session_events = await uow.events.list_after(session_id, 0, api.principal)

    assert dispatched == 1
    assert completed.json()["status"] == RunStatus.COMPLETED.value, completed.json()
    assert [reservation.payload["provider_call_id"] for reservation in reservations] == [CALL_ID]

    # The production roster: thirty definitions, the call reads among them, and
    # starting a call offered through tool.call rather than dropped.
    assert first_plan is not None and final_plan is not None
    assert len(first_plan.tool_names) == CHAT_TOOL_CAP
    assert "tool.call" in first_plan.tool_names
    assert set(first_plan.tool_names) >= CALL_READS
    assert START_CALL in first_plan.deferred_tool_names
    assert first_plan.skipped_tool_names == ()
    # Four restarted processes read the stored plan back and never re-planned.
    assert final_plan.epoch == first_plan.epoch
    assert final_plan.prefix_sha256 == first_plan.prefix_sha256
    assert not [e for e in session_events if e.event_type == "context.epoch.rotated"]

    # The provider saw tool.call; the pipeline proposed, approved and ran the call tool.
    call_events = [
        event
        for event in session_events
        if event.event_type.startswith("tool.call.")
        and event.payload.get("call_id") == "deferred-start-call"
    ]
    assert {event.payload["name"] for event in call_events} == {START_CALL}
    assert call_events[-1].event_type == "tool.call.completed"
    frames = _sse_frames(stream.text)
    ordered = [event for event, _data in frames]
    call_completed = next(
        index
        for index, (event, data) in enumerate(frames)
        if event == "tool.call.completed" and START_CALL in json.dumps(data)
    )
    assert ordered.count("run.waiting_for_user") == 2, ordered
    assert (
        ordered.index("run.waiting_for_user")
        < ordered.index("approval.requested")
        < ordered.index("approval.resolved")
        < call_completed
        < ordered.index("run.completed")
    ), ordered
