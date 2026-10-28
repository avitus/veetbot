"""ADR-0123: Chat defers tools past its item cap instead of dropping them.

The production-shaped roster below enables every flag production enables that
changes the default roster (People, Email mode, email unsubscribe, scheduling,
web, calling and the Scale VP bridge) and both Gmail accounts. Activating
another such flag in production adds it here first.
"""

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import SecretStr

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.bootstrap import Composition, build
from agent_core.config import load_settings
from agent_core.context.rendering import build_prefix, prefix_bytes
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
from agent_core.domain.tools import deferred_definition_text
from agent_core.mcp.configuration import email_server_configs
from agent_core.tools.cockpit import COCKPIT_TOOL_NAMES
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
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_deferred_tool_index import jsonb_key_order
from tests.unit.test_svp_composition import _discovery as _svp_discovery
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
# Production's flags that the roster above leaves off, because each moves its
# assertions: the hosted website browser, media generation, records and the Cockpit.
WEBSITE_AND_LATER_FLAGS = MappingProxyType(
    {
        "BROWSER_PROVIDER": "hosted",
        "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
        "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
        "TENSORSCALE_API_KEY": "synthetic-media-credential",
        "AGENT_RECORDS_ENABLED": "1",
        "AGENT_COCKPIT_ENABLED": "1",
    }
)
RECORDS_TOOLS = frozenset(
    f"records.{tool}" for tool in ("list", "query", "append", "create_series", "delete")
)
SVP_READS = frozenset(
    f"mcp.svp_read.{tool}" for tool in ("list_operations", "describe_operation", "call_operation")
)


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
    extra_scripts: Mapping[str, ScriptedMCPServer] = MappingProxyType({}),
    **build_kwargs: Any,
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
        svp_enabled=True,
        credentials={
            **loaded.credentials,
            "svp_read": SecretStr("/var/lib/veetbot/svp/credential.json"),
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
    scripts["svp_read"] = ScriptedMCPServer(name="svp_read", discovery=await _svp_discovery())
    scripts.update(extra_scripts)
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
        **build_kwargs,
    )


async def _session_events(app: Composition, session_id: Any) -> list[EventEnvelope]:
    async with app.uow_factory() as uow:
        return list(await uow.events.list_after(session_id, 0, app.principal))


async def _plan_for_one_turn(
    tmp_path: Path,
    *,
    enabled_tools: list[str] | None = None,
    extra_environment: Mapping[str, str] = MappingProxyType({}),
) -> ContextPlan:
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        enabled_tools=enabled_tools,
        extra_environment=extra_environment,
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
    assert len(discovered) == 22
    assert len(plan.tool_names) == 30
    assert "tool.call" in defined
    # The six management tools are deferred by the default agent's configuration.
    assert deferred >= MANAGEMENT_TOOLS | {"memory.learn", "workspace.write_text"}
    # A JSON field read waits in the index beside web.fetch's definition (ADR-0258).
    assert "web.fetch" in defined and "web.fetch_json" in deferred
    # Reads take the definition slots before anything that changes the world.
    assert defined >= DISCOVERED_READS
    # Deferring workspace writes admits one additional read while all writes
    # still follow the discovered reads (ADRs 0153 and 0233).
    assert defined & SVP_READS == {"mcp.svp_read.call_operation"}
    assert deferred >= SVP_READS - {"mcp.svp_read.call_operation"}
    writes = {
        spec.name for spec in discovered if spec.side_effect is not SideEffectClass.NETWORK_READ
    }
    assert START_CALL in writes
    assert writes <= deferred


async def test_the_cockpit_tools_are_index_entries_and_leave_the_definitions_alone(
    tmp_path: Path,
) -> None:
    """docs/plan/cockpit.md, the tools: the thirty-item definition roster is untouched."""

    default = await _plan_for_one_turn(tmp_path / "default")
    cockpit = await _plan_for_one_turn(
        tmp_path / "cockpit", extra_environment={"AGENT_COCKPIT_ENABLED": "1"}
    )
    assert cockpit.tool_names == default.tool_names
    assert cockpit.skipped_tool_names == ()
    assert set(cockpit.deferred_tool_names) - set(default.deferred_tool_names) == set(
        COCKPIT_TOOL_NAMES
    )


async def test_a_website_bound_chat_plans_with_every_production_flag_on(tmp_path: Path) -> None:
    """Production, 2026-10-25: once the Cockpit was on, a chat bound to a website
    profile failed before its first step, its definitions over the token cap."""

    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment=WEBSITE_AND_LATER_FLAGS,
    )
    async with context as app:
        await seed_browser_authority(app)
        created = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit("Make me a Cockpit widget.", created.id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        plan = await app.executor._context_planner.current(created.id)

    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    assert plan is not None
    # ADR-0130: the bound chat still defines its browser tools.
    assert set(plan.tool_names) >= {"browser.navigate", "browser.observe", "browser.act"}
    # The configured tools past the token cap wait in the index (ADR-0253).
    advertised = {*plan.tool_names, *plan.deferred_tool_names}
    assert advertised >= {*COCKPIT_TOOL_NAMES, *RECORDS_TOOLS}
    assert "records.append" in plan.deferred_tool_names
    # The index's 2,500-token cap holds every Gmail write beside web.fetch_json
    # (ADR-0262), so the bound chat offers everything it is configured with.
    assert plan.skipped_tool_names == ()
    assert {
        "mcp.gmail_write.modify_labels",
        "mcp.gmail_write.trash_thread",
        "mcp.gmail_write.untrash_thread",
    } <= set(plan.deferred_tool_names)


async def test_the_production_plan_renders_its_prefix_again_from_jsonb(tmp_path: Path) -> None:
    """The builder re-renders the plan event PostgreSQL returns, with object keys reordered."""
    _factory, context = await _production(
        tmp_path, [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)]
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("What can you do for me?", session_id)
        await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        async with app.uow_factory() as uow:
            session = await uow.sessions.get(session_id, app.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)
            event = await uow.events.latest_before(
                session_id, (1 << 63) - 1, "context.plan.created", app.principal
            )
    assert event is not None
    stored = ContextPlan.model_validate(jsonb_key_order(event.payload["plan"]))
    assert stored.deferred_tool_names

    prefix = build_prefix(
        agent,
        stored.tool_specs,
        stored.skill_catalog,
        stored.memory_snapshot,
        persona=stored.persona_text,
        deferred_tools=stored.deferred_tool_specs,
    )
    encoded = prefix_bytes(prefix, stored.tool_specs, stored.deferred_tool_specs)
    assert hashlib.sha256(encoded).hexdigest() == stored.prefix_sha256


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
    assert len(plan.skipped_tool_names) == len(legacy_tools) + 22 - 30


@pytest.mark.parametrize("owner_requests", [True, False])
async def test_a_deferred_call_gets_the_same_authorization(
    tmp_path: Path, owner_requests: bool
) -> None:
    """Deferral changes no authority: the owner's call needs no prompt (ADR-0213), and
    with that permission switched off it waits for the same approval as a direct call."""
    factory, context = await _production(
        tmp_path,
        [
            ScriptedTurn(
                tool_calls=[_tool_call(START_CALL, call_arguments())],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The call request was processed.", stop_reason=StopReason.END_TURN),
        ],
        extra_environment={"AGENT_CALL_OWNER_REQUESTS_ENABLED": "1" if owner_requests else "0"},
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit(
            f"Call {call_arguments()['phone_number']} to ask about ticket 12.", session_id
        )
        if not owner_requests:
            [approval] = await app.approvals.list_pending(run_id=run_id)
            assert approval.tool_name == START_CALL
            assert sum(client.call_count for client in factory.created) == 0
            await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        assert await app.approvals.list_pending(run_id=run_id) == []
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


@pytest.mark.parametrize(
    ("name", "trust"),
    [("schedule.pause", "internal_tool"), (START_CALL, "external_untrusted")],
)
async def test_invalid_deferred_arguments_return_the_whole_definition(
    tmp_path: Path, name: str, trust: str
) -> None:
    """ADR-0258: the index shows one sentence, so the refusal carries the rest.

    The production Cockpit chat of 2026-10-26 probed each Cockpit tool with
    empty arguments and got its schema back, never the description that says
    to read the kit first or that preview is unavailable.
    """
    _factory, context = await _production(
        tmp_path,
        [
            ScriptedTurn(tool_calls=[_tool_call(name, {})], stop_reason=StopReason.TOOL_USE),
            ScriptedTurn(text="I need more arguments.", stop_reason=StopReason.END_TURN),
        ],
    )
    async with context as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Use that tool.", session_id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        events = await _session_events(app, session_id)
        plan = await app.executor._context_planner.current(session_id)

    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    assert plan is not None
    [spec] = [spec for spec in plan.deferred_tool_specs if spec.name == name]
    [failure] = [event.payload for event in events if event.event_type == "tool.call.failed"]
    assert failure["reason_code"] == "tool.arguments_invalid"
    texts = [part["text"] for part in failure["result_item"]["content"]]
    # A server's or device's definition is its own text, so it stays untrusted data.
    assert failure["result_item"]["trust"] == trust
    assert texts[-1] == deferred_definition_text(spec)
    assert f"\nDescription: {spec.description}\n" in texts[-1]
