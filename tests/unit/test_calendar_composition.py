"""Read-only Calendar admission and scheduler authority (ADR-0305)."""

from __future__ import annotations

from pathlib import Path

from agent_core.config import load_settings
from tests.unit.test_config import base_environment


def test_calendar_is_default_off() -> None:
    assert getattr(load_settings(base_environment()), "calendar_enabled", None) is False


def test_calendar_has_one_read_only_server() -> None:
    import agent_core.mcp.configuration as configuration

    factory = getattr(configuration, "calendar_server_configs", None)
    assert callable(factory), "Calendar composition must expose its read-only server"
    assert factory("local", enabled=False) == ()
    (row,) = factory("local", enabled=True)
    assert row.server_id == "calendar_read"
    assert row.side_effect.value == "network_read"
    assert row.idempotency.value == "read_only"
    assert row.required_scopes == {"mcp.calendar_read.use"}


def test_private_credentials_and_scheduler_separation(tmp_path: Path) -> None:
    import json

    import pytest

    from agent_core.config import ConfigurationError, load_schedule_worker_settings
    from tests.contract.test_calendar_mcp_contract import credential

    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(credential()))
    path.chmod(0o600)
    environment = {
        **base_environment(),
        "AGENT_CALENDAR_ENABLED": "1",
        "CALENDAR_READ_CREDENTIAL_FILE": str(path),
    }
    settings = load_settings(environment)
    assert settings.calendar_enabled
    assert "calendar_read" in settings.credentials
    with pytest.raises(ConfigurationError):
        load_settings({**environment, "CALENDAR_READ_CREDENTIAL_FILE": ""})
    path.chmod(0o644)
    with pytest.raises(ConfigurationError):
        load_settings(environment)
    scheduler = load_schedule_worker_settings({**base_environment(), "AGENT_CALENDAR_ENABLED": "1"})
    assert scheduler.calendar_enabled and "calendar_read" not in scheduler.credentials
    with pytest.raises(ConfigurationError):
        load_schedule_worker_settings(environment)


def test_scheduler_vouches_for_calendar_scope() -> None:
    from dataclasses import replace

    from agent_core.bootstrap import _schedule_principal

    settings = load_settings(base_environment())
    assert "mcp.calendar_read.use" not in _schedule_principal(settings).scopes
    assert (
        "mcp.calendar_read.use"
        in _schedule_principal(replace(settings, calendar_enabled=True)).scopes
    )


def test_reads_use_existing_policy_and_selected_server_is_schedulable() -> None:
    from agent_core.domain.mcp import RESERVED_MCP_SERVER_PREFIXES, schedule_may_name
    from agent_core.domain.policies import PolicyDecisionType
    from agent_core.mcp.configuration import calendar_server_configs
    from agent_core.policy.engine import evaluate_deterministic
    from tests.gates.test_email_m18 import _action, _principal, _ruleset, _run

    (row,) = calendar_server_configs("local")
    scopes = set(row.required_scopes)
    result = evaluate_deterministic(
        _action(server_id=row.server_id, side_effect=row.side_effect, idempotency=row.idempotency),
        _principal().model_copy(update={"scopes": scopes}),
        _run().model_copy(update={"principal_scopes": scopes}),
        _ruleset(),
    )
    assert result.decision is PolicyDecisionType.ALLOW
    assert schedule_may_name(row)
    assert "calendar_" in RESERVED_MCP_SERVER_PREFIXES


async def test_chat_read_and_explicit_schedule_access(tmp_path: Path) -> None:
    import asyncio
    import json

    import pytest

    from agent_core.domain.errors import ScheduleValidationError
    from agent_core.domain.mcp import (
        MCPCallResult,
        MCPDiscovery,
        MCPRemoteTool,
        ScriptedMCPResponse,
        ScriptedMCPServer,
    )
    from agent_core.domain.messages import ScriptedToolCall, ScriptedTurn, StopReason
    from agent_core.domain.runs import RunStatus
    from agent_core.scheduling.defaults import derived_definition
    from calendar_mcp.client import CalendarClient
    from calendar_mcp.server import create_server
    from tests.contract.test_calendar_mcp_contract import credential
    from tests.gates.test_deferred_tools_adr0123 import _production
    from tests.gates.test_mcp_schedules_adr0291 import _once, _pins

    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(credential()))
    path.chmod(0o600)
    client = CalendarClient(json.dumps(credential()))
    try:
        tools = await create_server(client).list_tools()
    finally:
        await client.close()
    discovery = MCPDiscovery(
        tools=tuple(
            MCPRemoteTool(name=t.name, description=t.description or "", input_schema=t.input_schema)
            for t in tools
        )
    )
    scripts = {
        "calendar_read": ScriptedMCPServer(
            name="calendar_read",
            discovery=discovery,
            responses=(
                ScriptedMCPResponse(
                    name="list_calendars",
                    result=MCPCallResult(
                        structured={"calendars": [{"id": "primary"}], "complete": True}
                    ),
                ),
            ),
        )
    }
    turns = [
        ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name="tool.call",
                    arguments={"name": "mcp.calendar_read.list_calendars", "arguments": {}},
                    call_id="calendar",
                )
            ]
        ),
        ScriptedTurn(text="Your selected calendar is connected.", stop_reason=StopReason.END_TURN),
    ]
    factory, context = await _production(
        tmp_path,
        turns,
        extra_environment={
            "AGENT_CALENDAR_ENABLED": "1",
            "CALENDAR_READ_CREDENTIAL_FILE": str(path),
        },
        extra_scripts=scripts,
    )
    async with context as app:
        assert "mcp.calendar_read.use" in app.principal.scopes
        run_id = await app.runs.submit("List my calendars.")
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), 30)
        assert terminal.status is RunStatus.COMPLETED, terminal.failure
        assert await app.approvals.list_pending(run_id=run_id) == []
        assert (
            sum(
                client.call_count
                for client in factory.created
                if "CALENDAR_MCP_CREDENTIAL" in client.environment
            )
            == 1
        )
        definition = derived_definition(
            *_pins(app), title="Calendar briefing", instruction="Read my calendar.", cadence=_once()
        ).model_copy(update={"requested_scopes": frozenset({"mcp.calendar_read.use"})})
        with pytest.raises(ScheduleValidationError, match="Calendar"):
            await app.services.schedules.create(app.principal, definition, "unnamed-calendar")
        named = definition.model_copy(update={"mcp_servers": frozenset({"calendar_read"})})
        created = await app.services.schedules.create(app.principal, named, "named-calendar")
        assert created.revision.mcp_servers == {"calendar_read"}
        assert created.revision.requested_scopes == {"mcp.calendar_read.use"}


async def test_real_stdio_child_receives_only_broker_credential(tmp_path: Path) -> None:
    import json
    import shlex
    import sys

    from agent_core.adapters.mcp.sdk import SDKMCPClient
    from agent_core.domain.credentials import SecretValue
    from agent_core.mcp.configuration import build_stdio_environment, calendar_server_configs
    from tests.contract.test_calendar_mcp_contract import credential

    (row,) = calendar_server_configs("local")
    log = tmp_path / "calendar.stderr"
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "calendar_stdio_server.py"
    row = row.model_copy(update={"endpoint": shlex.join([sys.executable, str(fixture), str(log)])})
    raw = json.dumps(credential())
    environment = build_stdio_environment(row, SecretValue(raw))
    assert environment["CALENDAR_MCP_CREDENTIAL"] == raw
    assert raw not in row.endpoint
    async with SDKMCPClient(row, None, environment) as client:
        discovery = await client.discover()
        assert {tool.name for tool in discovery.tools} == {
            "list_calendars",
            "list_events",
            "get_event",
        }
        result = await client.call_tool("list_calendars", {})
        assert not result.is_error
        refused = await client.call_tool(
            "get_event", {"calendar_id": "unselected", "event_id": "event"}
        )
        assert refused.is_error
    output = log.read_text() + repr(result) + repr(refused)
    assert "secret-access" not in output
    assert "oauth2.googleapis.com" not in output
