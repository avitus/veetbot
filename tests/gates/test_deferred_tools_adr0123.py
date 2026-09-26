"""ADR-0123: Chat defers tools past its item cap instead of dropping them.

The production-shaped roster below enables every flag production enables that
changes the default roster (People, Email mode, email unsubscribe, scheduling,
web and calling) and both Gmail accounts. Activating another such flag in
production adds it here first.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import SecretStr

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.bootstrap import Composition, build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.context import ContextPlan
from agent_core.domain.events import EventEnvelope
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import SideEffectClass
from agent_core.domain.runs import RunStatus
from agent_core.mcp.configuration import email_server_configs
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
from tests.unit.test_web_tools import FakeWebProvider

MANAGEMENT_TOOLS = frozenset(
    {
        "schedule.update",
        "schedule.pause",
        "schedule.resume",
        "schedule.cancel",
        "email.subscriptions",
        "email.unsubscribe",
    }
)
DISCOVERED_READS = frozenset(
    {
        "mcp.bland_read.get_call",
        "mcp.bland_read.list_calls",
        *(
            f"mcp.{server}.{tool}"
            for server in ("gmail_read", "gmail_work_read")
            for tool in ("get_thread", "list_labels", "search_threads")
        ),
    }
)
START_CALL = "mcp.bland_call.start_call"


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


async def _production(
    tmp_path: Path,
    turns: Sequence[ScriptedTurn],
    *,
    enabled_tools: list[str] | None = None,
    extra_environment: Mapping[str, str] = MappingProxyType({}),
) -> tuple[ScriptedMCPClientFactory, AbstractAsyncContextManager[Composition]]:
    configuration = call_configuration()
    loaded = load_settings(
        {
            **_base_environment(),
            "AGENT_EMAIL_ENABLED": "1",
            "AGENT_EMAIL_MODE_ENABLED": "1",
            "GMAIL_ACCOUNTS_FILE": str(_accounts_manifest(tmp_path)),
            **extra_environment,
        }
    )
    settings = replace(
        loaded,
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
    factory = ScriptedMCPClientFactory(scripts)
    provider = FakeWebProvider()
    return factory, build(
        settings=settings,
        script=FakeModelScript(turns=list(turns)),
        mcp_client_factory=factory,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
        one_click_transport_override=Transport(),
        enabled_tools=enabled_tools,
    )


async def _session_events(app: Composition, session_id: Any) -> list[EventEnvelope]:
    async with app.uow_factory() as uow:
        return list(await uow.events.list_after(session_id, 0, app.principal))


async def _plan_for_one_turn(
    tmp_path: Path, *, enabled_tools: list[str] | None = None
) -> ContextPlan:
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        enabled_tools=enabled_tools,
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("What can you do for me?", session_id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        plan = await app.executor._context_planner.current(session_id)
    assert terminal.status is RunStatus.COMPLETED
    assert plan is not None
    return plan


def _tool_call(name: str, arguments: dict[str, Any]) -> ScriptedToolCall:
    return ScriptedToolCall(name="tool.call", arguments={"name": name, "arguments": arguments})


async def test_production_roster_defines_reads_first_and_defers_the_rest(tmp_path: Path) -> None:
    plan = await _plan_for_one_turn(tmp_path)

    defined = set(plan.tool_names)
    deferred = set(plan.deferred_tool_names)
    discovered = [spec for spec in plan.pinned_tool_specs if spec.name.startswith("mcp.")]
    # Nothing is dropped: every candidate has a definition or an index entry.
    assert plan.skipped_tool_names == ()
    assert len(discovered) == 19
    assert len(plan.tool_names) == 30
    assert "tool.call" in defined
    # The six management tools are deferred by the default agent's configuration.
    assert deferred >= MANAGEMENT_TOOLS
    # Reads take the definition slots before anything that changes the world.
    assert defined >= DISCOVERED_READS
    writes = {
        spec.name for spec in discovered if spec.side_effect is not SideEffectClass.NETWORK_READ
    }
    assert START_CALL in writes
    assert writes <= deferred


async def test_an_agent_without_tool_call_records_what_it_cannot_offer(tmp_path: Path) -> None:
    default = await _plan_for_one_turn(tmp_path / "default")
    legacy_tools = [
        name
        for name in (*default.tool_names, *default.deferred_tool_names)
        if not name.startswith("mcp.") and name != "tool.call"
    ]

    plan = await _plan_for_one_turn(tmp_path / "legacy", enabled_tools=legacy_tools)

    assert plan.deferred_tool_names == ()
    assert len(plan.tool_names) == 30
    assert "tool.call" not in plan.tool_names
    offered = {*plan.tool_names, *plan.skipped_tool_names}
    assert {spec.name for spec in default.pinned_tool_specs} - {"tool.call"} <= offered
    assert len(plan.skipped_tool_names) == len(legacy_tools) + 19 - 30


async def test_a_deferred_call_waits_for_the_same_approval(tmp_path: Path) -> None:
    factory, context = await _production(
        tmp_path,
        [
            ScriptedTurn(
                tool_calls=[_tool_call(START_CALL, call_arguments())],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The call request was processed.", stop_reason=StopReason.END_TURN),
        ],
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Call to ask about ticket 12.", session_id)
        [approval] = await app.approvals.list_pending(run_id=run_id)
        assert approval.tool_name == START_CALL
        assert sum(client.call_count for client in factory.created) == 0
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        events = await _session_events(app, session_id)
        async with app.uow_factory() as uow:
            reservations = await uow.calls.list(app.principal, "dispatch")

    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    assert sum(client.call_count for client in factory.created) == 1
    assert [reservation.payload["provider_call_id"] for reservation in reservations] == [CALL_ID]
    [response] = [event for event in events if event.event_type == "model.response.completed"][:1]
    [emitted] = response.payload["conversation_items"][-1:]
    # The provider replays the call the model made; the pipeline names the tool it ran.
    assert emitted["name"] == "tool.call"
    tool_events = [event for event in events if event.event_type.startswith("tool.call.")]
    assert {event.payload["name"] for event in tool_events} == {START_CALL}
    assert {event.payload["call_id"] for event in tool_events} == {emitted["call_id"]}
    assert tool_events[-1].event_type == "tool.call.completed"


async def test_tool_call_refuses_names_it_does_not_offer(tmp_path: Path) -> None:
    refused = ("mcp.nobody.nothing", "conversation.ask_user", "tool.call")
    _factory, context = await _production(
        tmp_path,
        [
            ScriptedTurn(
                tool_calls=[_tool_call(name, {}) for name in refused],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Those tools are not offered.", stop_reason=StopReason.END_TURN),
        ],
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Try some tools.", session_id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        events = await _session_events(app, session_id)

    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    denials = [event.payload for event in events if event.event_type == "tool.call.denied"]
    assert [payload["reason_code"] for payload in denials] == ["tool.not_found.not_offered"] * 3
    assert {payload["name"] for payload in denials} == {"tool.call"}
    # Each refusal answers the call the model made, so replay stays paired.
    assert all(payload["result_item"]["is_error"] for payload in denials)


async def test_invalid_deferred_arguments_return_the_input_schema(tmp_path: Path) -> None:
    _factory, context = await _production(
        tmp_path,
        [
            ScriptedTurn(
                tool_calls=[_tool_call("schedule.pause", {})],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I need a schedule id.", stop_reason=StopReason.END_TURN),
        ],
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Pause my schedule.", session_id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        events = await _session_events(app, session_id)

    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    [failure] = [event.payload for event in events if event.event_type == "tool.call.failed"]
    assert failure["name"] == "schedule.pause"
    assert failure["reason_code"] == "tool.arguments_invalid"
    texts = [part["text"] for part in failure["result_item"]["content"]]
    # A builtin's schema is trusted configuration, so the result keeps its trust.
    assert failure["result_item"]["trust"] == "internal_tool"
    assert any(text.startswith("Input schema: ") and '"schedule_id"' in text for text in texts)
