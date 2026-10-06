"""Composition admits the Scale VP bridge only through its flag (ADR-0153)."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.adapters.mcp.sdk import SDKMCPClient
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import (
    MCPAuthScheme,
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    MCPTransport,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import (
    IdempotencyClass,
    PolicyDecisionType,
    RiskLevel,
    SideEffectClass,
)
from agent_core.domain.runs import RunStatus
from svp_mcp.client import SvpClient
from svp_mcp.constants import CREDENTIAL_VARIABLE
from svp_mcp.credential import create_state_file, read_state
from svp_mcp.server import create_server
from tests.integration.m2_support import memory_settings
from tests.unit.test_config import base_environment

STATE_PATH = "/var/lib/veetbot/svp/credential.json"
TOOLS = {
    "mcp.svp_read.list_operations",
    "mcp.svp_read.describe_operation",
    "mcp.svp_read.call_operation",
}


def _rows(*, enabled: bool) -> tuple[Any, ...]:
    import agent_core.mcp.configuration as configuration

    factory = getattr(configuration, "svp_server_configs", None)
    assert callable(factory), "Scale VP server composition must be implemented"
    rows = factory("local", enabled=enabled)
    assert isinstance(rows, tuple)
    return rows


class _Tokens:
    def access_token(self, *, rejected: str | None = None) -> str:
        return "access-1"


async def _discovery() -> MCPDiscovery:
    catalog = create_server(
        SvpClient(
            _Tokens(),
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _request: httpx.Response(500))
            ),
        )
    )
    return MCPDiscovery(
        tools=tuple(
            MCPRemoteTool(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
            )
            for tool in await catalog.list_tools()
        )
    )


def test_the_bridge_is_default_off_and_the_platform_holds_only_a_private_path(
    tmp_path: Path,
) -> None:
    from agent_core.config import ConfigurationError, load_settings

    assert getattr(load_settings(base_environment()), "svp_enabled", None) is False
    state = tmp_path / "svp.json"
    state.write_text('{"refresh_token": "grant-material"}', encoding="utf-8")
    state.chmod(0o600)
    values = {**base_environment(), "AGENT_SVP_ENABLED": "1", "SVP_CREDENTIAL_FILE": str(state)}

    settings = load_settings(values)

    assert settings.svp_enabled is True
    assert settings.credentials["svp_read"].get_secret_value() == str(state)
    assert all(
        "grant-material" not in secret.get_secret_value()
        for secret in settings.credentials.values()
    )
    link = tmp_path / "link.json"
    link.symlink_to(state)
    for change in (
        {"SVP_CREDENTIAL_FILE": ""},
        {"SVP_CREDENTIAL_FILE": "relative.json"},
        {"SVP_CREDENTIAL_FILE": str(tmp_path / "absent.json")},
        {"SVP_CREDENTIAL_FILE": str(link)},
        {"SVP_READ_API_KEY": "a-second-source"},
    ):
        with pytest.raises(ConfigurationError):
            load_settings({**values, **change})
    state.chmod(0o644)
    with pytest.raises(ConfigurationError):
        load_settings(values)
    with pytest.raises(ConfigurationError, match="AGENT_SVP_ENABLED"):
        load_settings({**base_environment(), "SVP_CREDENTIAL_FILE": str(state)})


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_grant_the_service_could_not_rewrite_is_refused_at_startup(tmp_path: Path) -> None:
    """The bridge rewrites the grant beside a lock file, so its directory must be writable."""
    from agent_core.config import ConfigurationError, load_settings

    directory = tmp_path / "svp"
    directory.mkdir()
    state = directory / "credential.json"
    state.write_text("{}", encoding="utf-8")
    state.chmod(0o600)
    values = {**base_environment(), "AGENT_SVP_ENABLED": "1", "SVP_CREDENTIAL_FILE": str(state)}
    assert load_settings(values).svp_enabled is True

    directory.chmod(0o500)
    try:
        with pytest.raises(ConfigurationError, match="SVP_CREDENTIAL_FILE"):
            load_settings(values)
    finally:
        directory.chmod(0o700)


def test_a_role_that_refuses_provider_credentials_refuses_the_grant_file(tmp_path: Path) -> None:
    from agent_core.config import ConfigurationError, load_surface_worker_settings

    telegram = tmp_path / "telegram-token"
    telegram.write_text("telegram-test-token-with-at-least-32-chars")
    telegram.chmod(0o600)
    environment = {
        **base_environment(),
        "AGENT_SURFACE_API_ENABLED": "1",
        "AGENT_SURFACE_WORKER_ENABLED": "1",
        "AGENT_SURFACE_TELEGRAM_TOKEN_FILE": str(telegram),
    }
    assert load_surface_worker_settings(environment).credentials == {}

    with pytest.raises(ConfigurationError, match="SVP_CREDENTIAL_FILE"):
        load_surface_worker_settings({**environment, "SVP_CREDENTIAL_FILE": STATE_PATH})


def test_the_row_is_one_read_only_operator_server_that_receives_only_the_path() -> None:
    import agent_core.mcp.configuration as configuration

    assert _rows(enabled=False) == ()
    (row,) = _rows(enabled=True)

    assert row.server_id == "svp_read"
    assert row.transport is MCPTransport.STDIO and row.operator_configured is True
    assert row.endpoint == shlex.join([sys.executable, "-m", "svp_mcp", "--mode", "read"])
    assert row.auth_scheme is MCPAuthScheme.ENV
    assert row.auth_name == CREDENTIAL_VARIABLE
    assert row.credential_ref == "svp_read"
    assert row.side_effect is SideEffectClass.NETWORK_READ
    assert row.risk is RiskLevel.LOW
    assert row.idempotency is IdempotencyClass.READ_ONLY
    assert row.required_scopes == {"mcp.svp_read.use"}
    configuration.validate_mcp_config(row, destination_allowed=lambda _url: False)
    environment = configuration.build_stdio_environment(row, SecretValue(STATE_PATH))
    assert environment[CREDENTIAL_VARIABLE] == STATE_PATH


def test_a_bridge_read_needs_no_approval_under_the_shipped_policy() -> None:
    from agent_core.policy.engine import evaluate_deterministic
    from tests.gates.test_email_m18 import _action, _principal, _ruleset, _run

    (row,) = _rows(enabled=True)
    scopes = set(row.required_scopes)
    decision = evaluate_deterministic(
        _action(server_id=row.server_id, side_effect=row.side_effect, idempotency=row.idempotency),
        _principal().model_copy(update={"scopes": scopes}),
        _run().model_copy(update={"principal_scopes": scopes}),
        _ruleset(),
    )

    assert decision.decision is PolicyDecisionType.ALLOW


async def test_the_flag_offers_the_owner_three_tools_and_a_call_reaches_the_bridge() -> None:
    from agent_core.bootstrap import build

    assert "svp_enabled" in memory_settings().__dataclass_fields__, "the flag must be a setting"
    factory = ScriptedMCPClientFactory(
        {
            "svp_read": ScriptedMCPServer(
                name="svp_read",
                discovery=await _discovery(),
                responses=(
                    ScriptedMCPResponse(
                        name="list_operations",
                        result=MCPCallResult(structured={"operations": [], "truncated": False}),
                    ),
                ),
            )
        }
    )
    settings = replace(
        memory_settings(),
        svp_enabled=True,
        credentials={"svp_read": SecretStr(STATE_PATH)},
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="mcp.svp_read.list_operations", arguments={})],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Those are the operations.", stop_reason=StopReason.END_TURN),
        ]
    )

    async with build(
        settings=settings, script=script, sequential_ids=True, mcp_client_factory=factory
    ) as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("What can you read from Scale VP?", session_id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        plan = await app.executor._context_planner.current(session_id)
        assert "mcp.svp_read.use" in app.principal.scopes

    assert terminal.status is RunStatus.COMPLETED
    assert plan is not None
    assert {*plan.tool_names, *plan.deferred_tool_names} >= TOOLS
    assert sum(client.call_count for client in factory.created) == 1


async def test_without_the_flag_no_bridge_row_or_scope_is_composed() -> None:
    from agent_core.bootstrap import build
    from agent_core.config import ConfigurationError

    script = FakeModelScript(turns=[ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN)])
    async with build(settings=memory_settings(), script=script) as app:
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Hello.", session_id)
        await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        plan = await app.executor._context_planner.current(session_id)
        assert "mcp.svp_read.use" not in app.principal.scopes
    assert plan is not None
    assert not TOOLS & {*plan.tool_names, *plan.deferred_tool_names}

    (row,) = _rows(enabled=True)
    with pytest.raises(ConfigurationError, match="AGENT_SVP_ENABLED"):
        async with build(settings=memory_settings(), script=script, mcp_servers=(row,)):
            pass


async def test_the_real_child_rotates_its_own_grant_and_journals_no_request(tmp_path: Path) -> None:
    """Under the exact environment the platform builds, only the child touches the grant."""
    import agent_core.mcp.configuration as configuration

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "svp_stdio_server.py"
    stderr_path = tmp_path / "svp-read.stderr"
    state_path = tmp_path / "state" / "svp.json"
    secret = "-".join(("client", "value"))
    create_state_file(
        state_path,
        {
            "version": 1,
            "client_id": "client-1",
            "client_secret": secret,
            "refresh_token": "refresh-1",
            "access_token": "access-1",
            "expires_at": 0,
        },
    )
    (row,) = _rows(enabled=True)
    config = row.model_copy(
        update={"endpoint": shlex.join([sys.executable, str(fixture), str(stderr_path)])}
    )
    environment = configuration.build_stdio_environment(config, SecretValue(str(state_path)))

    async with SDKMCPClient(config, None, environment) as client:
        discovery = await client.discover()
        read = await client.call_tool(
            "call_operation", {"operation_id": "list_companies", "query": {"limit": 5}}
        )

    assert {tool.name for tool in discovery.tools} == {name.rsplit(".", 1)[1] for name in TOOLS}
    assert not read.is_error
    assert read.structured == {"data": {"items": ["Acme"]}}
    assert read_state(state_path)["refresh_token"] == "refresh-2"
    stderr = stderr_path.read_text(encoding="utf-8")
    assert "svp child capture probe" in stderr
    for value in ("HTTP Request", "scalevp-mcp.com", "/companies", "limit", "access-", "refresh-"):
        assert value not in stderr
    assert secret not in stderr
