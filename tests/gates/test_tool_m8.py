"""Milestone 8 MCP/tool-pipeline hard gates."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.adapters.mcp.sdk import SDKMCPClient, SDKMCPClientFactory, _unauthorized
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.credentials import CredentialRef, SecretValue
from agent_core.domain.errors import NotFoundError
from agent_core.domain.mcp import (
    MCPAuthScheme,
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    MCPServerConfig,
    MCPTransport,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolResult,
    ToolSource,
    ToolSpec,
)
from agent_core.evals.cases import load_cases
from agent_core.evals.runner import run_case
from agent_core.mcp.configuration import build_stdio_environment, validate_mcp_config
from agent_core.mcp.mapping import map_discovered_tools
from agent_core.ports.mcp import MCPClientFactory
from agent_core.tools.executor import PIPELINE_STEP_SEQUENCE
from agent_core.tools.registry import StaticToolRegistry
from scripts.architecture_checks import architecture_errors
from tests.contract.support import tool_context

ROOT = Path(__file__).resolve().parents[2]


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/tool-m8",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
    )


def _server(
    server_id: str,
    *,
    side_effect: SideEffectClass = SideEffectClass.NONE,
    idempotency: IdempotencyClass = IdempotencyClass.IDEMPOTENT,
    credential_ref: str | None = None,
) -> MCPServerConfig:
    return MCPServerConfig(
        tenant_id="local",
        server_id=server_id,
        transport=MCPTransport.STDIO,
        endpoint=f"/fixture/{server_id}",
        operator_configured=True,
        auth_scheme=MCPAuthScheme.ENV if credential_ref else MCPAuthScheme.NONE,
        auth_name="MCP_TOKEN" if credential_ref else None,
        credential_ref=credential_ref,
        side_effect=side_effect,
        risk=RiskLevel.LOW,
        idempotency=idempotency,
    )


def _discovery() -> MCPDiscovery:
    return MCPDiscovery(
        tools=(
            MCPRemoteTool(
                name="echo",
                description="Echo a value.",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
        )
    )


class _TenantTool:
    def __init__(self, spec: ToolSpec, marker: str) -> None:
        self.spec = spec
        self.marker = marker

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments, context
        return ToolResult(ok=True, content=[TextPart(text=self.marker)])


class _TrackedClient:
    def __init__(self, *, fail_discovery: bool, fail_close: bool = False) -> None:
        self.fail_discovery = fail_discovery
        self.fail_close = fail_close
        self.entered = False
        self.closed = False

    async def __aenter__(self) -> _TrackedClient:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.entered = False
        self.closed = True
        if self.fail_close:
            raise RuntimeError("injected MCP close failure")

    async def discover(self) -> MCPDiscovery:
        if self.fail_discovery:
            raise RuntimeError("unexpected discovery failure")
        return _discovery()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
        del name, arguments
        return MCPCallResult()

    async def read_resource(self, uri: str | None) -> MCPCallResult:
        del uri
        return MCPCallResult()

    async def reauthenticate(
        self, credential: SecretValue | None, environment: dict[str, str]
    ) -> bool:
        del credential, environment
        return False


class _TrackedFactory:
    def __init__(
        self,
        *,
        fail_discovery: frozenset[str] = frozenset({"second"}),
        fail_close: frozenset[str] = frozenset(),
    ) -> None:
        self.clients: list[_TrackedClient] = []
        self.fail_discovery = fail_discovery
        self.fail_close = fail_close

    def __call__(
        self,
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> _TrackedClient:
        del credential, environment
        client = _TrackedClient(
            fail_discovery=config.server_id in self.fail_discovery,
            fail_close=config.server_id in self.fail_close,
        )
        self.clients.append(client)
        return client


class _DiscoveryBarrier:
    def __init__(self, expected: int, *, hold_until_released: bool = False) -> None:
        self.expected = expected
        self.hold_until_released = hold_until_released
        self.started = 0
        self.in_flight = 0
        self.maximum_in_flight = 0
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()


class _BarrierClient(_TrackedClient):
    def __init__(self, barrier: _DiscoveryBarrier) -> None:
        super().__init__(fail_discovery=False)
        self.barrier = barrier

    async def __aenter__(self) -> _BarrierClient:
        self.entered = True
        return self

    async def discover(self) -> MCPDiscovery:
        self.barrier.started += 1
        self.barrier.in_flight += 1
        self.barrier.maximum_in_flight = max(
            self.barrier.maximum_in_flight,
            self.barrier.in_flight,
        )
        if self.barrier.started == self.barrier.expected:
            self.barrier.all_started.set()
        try:
            if self.barrier.hold_until_released:
                await self.barrier.release.wait()
            else:
                await asyncio.wait_for(self.barrier.all_started.wait(), timeout=0.2)
        except TimeoutError:
            pass
        finally:
            self.barrier.in_flight -= 1
        return _discovery()


class _BarrierFactory:
    def __init__(self, expected: int, *, hold_until_released: bool = False) -> None:
        self.barrier = _DiscoveryBarrier(
            expected,
            hold_until_released=hold_until_released,
        )
        self.clients: list[_BarrierClient] = []

    def __call__(
        self,
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> _BarrierClient:
        del config, credential, environment
        client = _BarrierClient(self.barrier)
        self.clients.append(client)
        return client


async def test_dynamic_registry_is_tenant_scoped() -> None:
    spec = map_discovered_tools(_server("shared"), _discovery().tools).accepted[0].spec
    first = _TenantTool(spec, "first")
    second = _TenantTool(spec, "second")
    registry = StaticToolRegistry()
    registry.register_dynamic(first, tenant_id="tenant-one")
    registry.register_dynamic(second, tenant_id="tenant-two")
    first_result = await registry.get(spec.name, tenant_id="tenant-one").execute({}, tool_context())
    second_result = await registry.get(spec.name, tenant_id="tenant-two").execute(
        {}, tool_context()
    )
    assert first_result.content == [TextPart(text="first")]
    assert second_result.content == [TextPart(text="second")]
    with pytest.raises(NotFoundError):
        registry.get(spec.name)


def test_untrusted_schema_walk_is_iterative_and_bounded() -> None:
    schema: dict[str, object] = {"type": "object"}
    current = schema
    for _ in range(2_000):
        nested: dict[str, object] = {}
        current["properties"] = nested
        current = nested
    remote = MCPRemoteTool.model_construct(name="deep", description="", input_schema=schema)
    report = map_discovered_tools(_server("deep"), (remote,))
    assert report.accepted == ()
    assert report.rejected == ("deep",)


def test_unauthorized_finds_nested_exception_groups_without_cycles() -> None:
    unauthorized = RuntimeError("unauthorized")
    unauthorized.status_code = 401  # type: ignore[attr-defined]
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [unauthorized])])
    unauthorized.__cause__ = group
    assert _unauthorized(group)


async def test_oauth_reauthentication_refreshes_an_unchanged_client_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPServerConfig.model_validate(
        {
            "tenant_id": "local",
            "server_id": "oauth_refresh",
            "transport": MCPTransport.HTTP,
            "endpoint": "https://allowed.test/mcp",
            "auth_scheme": MCPAuthScheme.OAUTH2_CLIENT,
            "credential_ref": "oauth-ref",
            "token_endpoint": "https://allowed.test/token",
        }
    )
    credential = SecretValue('{"client_id":"id","client_secret":"secret"}')
    client = SDKMCPClient(config, credential, {}, http_proxy_url="http://127.0.0.1:1")
    calls: list[str] = []

    async def close(*_args: object) -> None:
        calls.append("close")

    async def connect() -> None:
        calls.append("connect")

    monkeypatch.setattr(client, "_close", close)
    monkeypatch.setattr(client, "_connect", connect)
    assert await client.reauthenticate(credential, {})
    assert calls == ["close", "connect"]


async def test_oauth_token_exchange_disables_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPServerConfig(
        tenant_id="local",
        server_id="oauth_redirect",
        transport=MCPTransport.HTTP,
        endpoint="https://allowed.test/mcp",
        auth_scheme=MCPAuthScheme.OAUTH2_CLIENT,
        credential_ref="oauth-ref",
        token_endpoint="/".join(("https:", "", "allowed.test", "token")),
    )
    observed: dict[str, object] = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, str]:
            return {"access_token": "fixture-access-token"}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return

        async def post(self, url: str, **_kwargs: object) -> _Response:
            observed["url"] = url
            return _Response()

    monkeypatch.setattr("agent_core.adapters.mcp.sdk.httpx2.AsyncClient", _Client)
    client = SDKMCPClient(
        config,
        SecretValue('{"client_id":"id","client_secret":"secret"}'),
        {},
    )
    assert await client._exchange_client_token() == "fixture-access-token"
    assert observed["follow_redirects"] is False
    assert observed["url"] == "https://allowed.test/token"


async def test_prepare_closes_all_clients_after_unexpected_discovery_failure() -> None:
    factory = _TrackedFactory()
    with pytest.raises(RuntimeError, match="unexpected discovery failure"):
        async with build(
            settings=_settings(),
            sequential_ids=True,
            mcp_servers=(_server("first"), _server("second")),
            mcp_client_factory=factory,
        ) as composition:
            await composition.sessions.create()
    assert len(factory.clients) == 2
    assert all(client.closed and not client.entered for client in factory.clients)


async def test_cancelled_prepare_finishes_in_flight_client_cleanup() -> None:
    """Cancelling the parent gather twice must not abandon an entered client."""

    discovery_started = asyncio.Event()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    closed = asyncio.Event()

    class CancellationClient(_TrackedClient):
        async def discover(self) -> MCPDiscovery:
            discovery_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def __aexit__(self, *_args: object) -> None:
            close_started.set()
            await allow_close.wait()
            self.entered = False
            self.closed = True
            closed.set()

    client = CancellationClient(fail_discovery=False)

    def factory(
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> CancellationClient:
        del config, credential, environment
        return client

    async with build(
        settings=_settings(),
        sequential_ids=True,
        mcp_servers=(_server("cancelled"),),
        mcp_client_factory=cast(MCPClientFactory, factory),
    ) as composition:
        startup = asyncio.create_task(composition.sessions.create())
        await asyncio.wait_for(discovery_started.wait(), timeout=1)
        startup.cancel()
        await asyncio.wait_for(close_started.wait(), timeout=1)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        allow_close.set()
        await asyncio.wait_for(closed.wait(), timeout=1)

    assert client.closed and not client.entered


async def test_prepare_discovers_independent_servers_concurrently() -> None:
    factory = _BarrierFactory(expected=3)
    async with build(
        settings=_settings(),
        sequential_ids=True,
        mcp_servers=(_server("first"), _server("second"), _server("third")),
        mcp_client_factory=factory,
    ) as composition:
        await composition.sessions.create()
    assert factory.barrier.maximum_in_flight == 3


async def test_prepare_bounds_server_discovery_fan_out() -> None:
    factory = _BarrierFactory(expected=8, hold_until_released=True)
    configs = tuple(_server(f"server_{index}") for index in range(9))
    async with build(
        settings=_settings(),
        sequential_ids=True,
        mcp_servers=configs,
        mcp_client_factory=factory,
    ) as composition:
        startup = asyncio.create_task(composition.sessions.create())
        try:
            await asyncio.wait_for(factory.barrier.all_started.wait(), timeout=1)
            assert factory.barrier.started == 8
        finally:
            factory.barrier.release.set()
            await startup
    assert factory.barrier.maximum_in_flight == 8


async def test_dynamic_registrations_are_owned_by_live_sessions() -> None:
    config = _server("owned")
    scripted = ScriptedMCPServer(name="owned", discovery=_discovery())
    async with build(
        settings=_settings(),
        sequential_ids=True,
        mcp_servers=(config,),
        mcp_client_factory=ScriptedMCPClientFactory({"owned": scripted}),
    ) as composition:
        first = await composition.sessions.create()
        second = await composition.sessions.create()
        name = "mcp.owned.echo"
        assert composition.tool_pipeline._registry.get(name, tenant_id="local")
        await composition.services.sessions.close(composition.principal, first)
        assert composition.tool_pipeline._registry.get(name, tenant_id="local")
        await composition.services.sessions.close(composition.principal, second)
        with pytest.raises(NotFoundError):
            composition.tool_pipeline._registry.get(name, tenant_id="local")


async def test_mcp_close_isolates_connection_failures() -> None:
    factory = _TrackedFactory(
        fail_discovery=frozenset(),
        fail_close=frozenset({"first"}),
    )
    async with build(
        settings=_settings(),
        sequential_ids=True,
        mcp_servers=(_server("first"), _server("second")),
        mcp_client_factory=factory,
    ) as composition:
        session_id = await composition.sessions.create()
        await composition.mcp.close_session(session_id)
        for name in ("mcp.first.echo", "mcp.second.echo"):
            with pytest.raises(NotFoundError):
                composition.tool_pipeline._registry.get(name, tenant_id="local")
    assert len(factory.clients) == 2
    assert all(client.closed for client in factory.clients)


async def test_session_transaction_rollback_discards_ephemeral_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _server("rollback")
    scripted = ScriptedMCPServer(name="rollback", discovery=_discovery())
    async with build(
        settings=_settings(),
        sequential_ids=True,
        mcp_servers=(config,),
        mcp_client_factory=ScriptedMCPClientFactory({"rollback": scripted}),
    ) as composition:
        internal_session_id = None
        with pytest.raises(RuntimeError, match="force internal rollback"):
            async with composition.uow_factory() as uow:
                internal_session_id = await composition.sessions.create_in(uow)
                assert composition.skill_catalogs.current(internal_session_id)
                raise RuntimeError("force internal rollback")
        assert internal_session_id is not None
        with pytest.raises(NotFoundError):
            composition.skill_catalogs.current(internal_session_id)
        with pytest.raises(NotFoundError):
            composition.tool_pipeline._registry.get("mcp.rollback.echo", tenant_id="local")

        async with composition.uow_factory() as uow:
            event_repository = uow.events

        async def fail_session_event(_event: object) -> None:
            raise RuntimeError("force public rollback")

        monkeypatch.setattr(event_repository, "append", fail_session_event)
        with pytest.raises(RuntimeError, match="force public rollback"):
            await composition.services.sessions.create(
                composition.principal,
                "general",
                {},
            )
        assert not composition.skill_catalogs._catalogs
        with pytest.raises(NotFoundError):
            composition.tool_pipeline._registry.get("mcp.rollback.echo", tenant_id="local")


async def test_mcp_pipeline_parity() -> None:
    config = _server("parity")
    scripted = ScriptedMCPServer(
        name="parity",
        discovery=_discovery(),
        responses=(
            ScriptedMCPResponse(
                name="echo",
                result=MCPCallResult(content=("external",)),
            ),
        ),
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="math.calculate", arguments={"expression": "2 + 2"})
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="mcp.parity.echo", arguments={"value": "external"})
                ]
            ),
            ScriptedTurn(text="done"),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        sequential_ids=True,
        enabled_tools=["math.calculate", "mcp.parity.echo"],
        mcp_servers=(config,),
        mcp_client_factory=ScriptedMCPClientFactory({"parity": scripted}),
    ) as composition:
        run_id = await composition.runs.submit("exercise both tool sources")
        await composition.runs.wait_terminal(run_id)
        traces = composition.tool_pipeline.completed_traces(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
    assert [trace.tool_source for trace in traces] == [ToolSource.BUILTIN, ToolSource.MCP]
    assert traces[0].steps == traces[1].steps == PIPELINE_STEP_SEQUENCE
    assert len(traces[0].steps) == 14
    mcp_invocation = next(item for item in invocations if item.tool_source is ToolSource.MCP)
    assert mcp_invocation.result_item is not None
    assert mcp_invocation.result_item.trust.value == "external_untrusted"


async def test_mcp_disconnect() -> None:
    case = next(
        item
        for item in load_cases(ROOT / "tests" / "eval_cases")
        if item.name == "mcp_server_disconnects_mid_call"
    )
    result = await run_case(case, ROOT / "evals" / "fixtures" / "models")
    failure = next(
        event
        for event in result.events
        if event.event_type == "tool.call.failed"
        and event.payload.get("reason_code") == "tool.server_unreachable"
    )
    outcome = ToolOutcome.model_validate_json(failure.payload["result_item"]["content"][0]["text"])
    assert outcome.status is ToolOutcomeStatus.UNAVAILABLE
    assert result.run.final_message == "Continued after the server became unavailable."


def test_mcp_sdk_confined(tmp_path: Path) -> None:
    errors = architecture_errors(ROOT)
    assert not [error for error in errors if "MCP SDK" in error]
    violating = tmp_path / "src" / "agent_core" / "runtime" / "violation.py"
    violating.parent.mkdir(parents=True)
    violating.write_text("import mcp\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    assert any(
        "MCP SDK mcp crosses adapter boundary" in error for error in architecture_errors(tmp_path)
    )


def test_mcp_auth_config() -> None:
    with pytest.raises(ValidationError):
        MCPServerConfig.model_validate(
            {
                **_server("bad").model_dump(mode="json"),
                "auth_scheme": "invented",
            }
        )
    invalid_rows: list[dict[str, object]] = [
        {
            "transport": MCPTransport.HTTP,
            "operator_configured": False,
            "endpoint": "https://example.test/mcp",
            "auth_scheme": MCPAuthScheme.ENV,
            "auth_name": "TOKEN",
            "credential_ref": "ref",
        },
        {"auth_scheme": MCPAuthScheme.NONE, "credential_ref": "ref"},
        {"auth_scheme": MCPAuthScheme.ENV, "auth_name": None, "credential_ref": "ref"},
        {
            "transport": MCPTransport.HTTP,
            "operator_configured": False,
            "endpoint": "https://example.test/mcp",
            "auth_scheme": MCPAuthScheme.HEADER,
            "auth_name": None,
            "credential_ref": "ref",
        },
        {
            "transport": MCPTransport.HTTP,
            "operator_configured": False,
            "endpoint": "https://example.test/mcp",
            "auth_scheme": MCPAuthScheme.HEADER,
            "auth_name": "Authorization",
            "credential_ref": "ref",
        },
    ]
    base: dict[str, object] = _server("invalid").model_dump(mode="python")
    for row in invalid_rows:
        with pytest.raises(ValidationError):
            MCPServerConfig.model_validate({**base, **row})
    for endpoint in ("http://allowed.test/mcp", "/relative/mcp"):
        with pytest.raises(ValidationError, match="HTTPS"):
            MCPServerConfig.model_validate(
                {
                    **base,
                    "transport": MCPTransport.HTTP,
                    "endpoint": endpoint,
                    "operator_configured": False,
                    "auth_scheme": MCPAuthScheme.BEARER,
                    "credential_ref": "ref",
                }
            )
    tier_zero = MCPServerConfig.model_validate(
        {
            **base,
            "auth_scheme": MCPAuthScheme.ENV,
            "auth_name": "VEETBOT_OPENAI_KEY",
            "credential_ref": "ref",
        }
    )
    with pytest.raises(ValueError, match="tier-0"):
        validate_mcp_config(tier_zero, destination_allowed=lambda _url: False)
    oauth = MCPServerConfig.model_validate(
        {
            "tenant_id": "local",
            "server_id": "oauth",
            "transport": MCPTransport.HTTP,
            "endpoint": "https://allowed.test/mcp",
            "auth_scheme": MCPAuthScheme.OAUTH2_CLIENT,
            "credential_ref": "oauth-ref",
            "token_endpoint": "https://denied.test/token",
        }
    )
    with pytest.raises(ValueError, match="token endpoint"):
        validate_mcp_config(
            oauth,
            destination_allowed=lambda url: url.startswith("https://allowed.test/"),
        )
    with pytest.raises(ValidationError, match="HTTPS"):
        MCPServerConfig.model_validate(
            {
                **oauth.model_dump(mode="python"),
                "token_endpoint": "http://allowed.test/token",
            }
        )


class _RotatingCredentials:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    async def resolve(self, reference: CredentialRef) -> SecretValue:
        count = self.calls.get(reference.name, 0)
        self.calls[reference.name] = count + 1
        return SecretValue("old" if count == 0 else "new")


async def test_mcp_reauth_bounded() -> None:
    read = _server("read", credential_ref="read-ref")
    write = _server(
        "write",
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        credential_ref="write-ref",
    )
    read_script = ScriptedMCPServer(
        name="read",
        discovery=_discovery(),
        responses=(
            ScriptedMCPResponse(name="echo", outcome="unauthorized"),
            ScriptedMCPResponse(name="echo", result=MCPCallResult(content=("retried",))),
            ScriptedMCPResponse(name="echo", outcome="unauthorized"),
        ),
    )
    write_script = ScriptedMCPServer(
        name="write",
        discovery=_discovery(),
        responses=(
            ScriptedMCPResponse(name="echo", outcome="unauthorized"),
            ScriptedMCPResponse(name="echo", result=MCPCallResult(content=("must not retry",))),
        ),
    )
    factory = ScriptedMCPClientFactory({"read": read_script, "write": write_script})
    credentials = _RotatingCredentials()
    async with build(
        settings=_settings(),
        sequential_ids=True,
        mcp_servers=(read, write),
        mcp_client_factory=factory,
        credential_resolver=credentials,
    ) as composition:
        session_id = await composition.sessions.create()
        context = replace(tool_context(), session_id=session_id, tenant_id="local")
        read_spec = map_discovered_tools(read, _discovery().tools).accepted[0].spec
        write_spec = map_discovered_tools(write, _discovery().tools).accepted[0].spec
        first = await composition.mcp.call_tool(context, read_spec, "echo", {"value": "one"})
        uncertain = await composition.mcp.call_tool(
            context, write_spec, "echo", {"value": "effect"}
        )
        exhausted = await composition.mcp.call_tool(context, read_spec, "echo", {"value": "two"})
    assert first.ok and isinstance(first.content[0], TextPart)
    assert first.content[0].text == "retried"
    assert uncertain.failure is not None
    assert uncertain.failure.reason_code == "tool.outcome_unknown"
    assert exhausted.failure is not None
    assert exhausted.failure.reason_code == "tool.server_unauthorized"
    assert [client.reauthentication_count for client in factory.created] == [1, 1]
    assert [client.call_count for client in factory.created] == [3, 1]

    run_server = _server("run_read", credential_ref="run-read-ref")
    run_script = ScriptedMCPServer(
        name="run_read",
        discovery=_discovery(),
        responses=(
            ScriptedMCPResponse(name="echo", outcome="unauthorized"),
            ScriptedMCPResponse(name="echo", result=MCPCallResult(content=("recovered",))),
        ),
    )
    run_factory = ScriptedMCPClientFactory({"run_read": run_script})
    async with build(
        settings=_settings(),
        script=FakeModelScript(
            turns=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="mcp.run_read.echo",
                            arguments={"value": "continue"},
                        )
                    ]
                ),
                ScriptedTurn(text="continued after reauthentication"),
            ]
        ),
        sequential_ids=True,
        enabled_tools=["mcp.run_read.echo"],
        mcp_servers=(run_server,),
        mcp_client_factory=run_factory,
        credential_resolver=_RotatingCredentials(),
    ) as composition:
        run_id = await composition.runs.submit("recover and continue")
        completed = await composition.runs.wait_terminal(run_id)
    assert completed.final_message == "continued after reauthentication"
    assert run_factory.created[0].reauthentication_count == 1


async def test_mcp_stdio_env_built(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel_name = "M8_WORKER_SENTINEL"
    monkeypatch.setenv(sentinel_name, "must-not-cross")
    server_path = ROOT / "tests" / "fixtures" / "mcp_stdio_environment_server.py"
    config = MCPServerConfig(
        tenant_id="local",
        server_id="environment",
        transport=MCPTransport.STDIO,
        endpoint=f"{shlex.quote(sys.executable)} {shlex.quote(str(server_path))}",
        operator_configured=True,
        auth_scheme=MCPAuthScheme.ENV,
        auth_name="MCP_TOKEN",
        credential_ref="fixture-token",
    )
    credential = SecretValue("fixture-value")
    with pytest.raises(ValueError, match="tier-0"):
        build_stdio_environment(
            config,
            credential,
            synthesized={"VEETBOT_OPENAI_KEY": "must-not-cross"},
        )
    environment = build_stdio_environment(config, credential)
    client = SDKMCPClientFactory()(config, credential, environment)
    async with client:
        discovery = await client.discover()
        assert [tool.name for tool in discovery.tools] == ["echo_environment"]
        result = await client.call_tool("echo_environment", {})
    echoed = json.loads(result.content[0])
    # macOS injects this one UI-encoding variable below the process API even
    # when exec receives an exact environment; Linux (including CI) does not.
    platform_injected = echoed.pop("__CF_USER_TEXT_ENCODING", None)
    if sys.platform == "darwin":
        assert isinstance(platform_injected, str)
    else:
        assert platform_injected is None
    assert echoed == environment
    assert echoed["MCP_TOKEN"] == "fixture-value"  # noqa: S105, RUF100
    assert sentinel_name not in echoed
    assert "VEETBOT_OPENAI_KEY" not in echoed
    assert set(echoed) == {"HOME", "PWD", "PATH", "TMPDIR", "LANG", "MCP_TOKEN"}
    assert os.environ[sentinel_name] == "must-not-cross"
