"""ADR-0255 owner-added MCP servers: the Settings routes, sign-in, and the runtime."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import (
    MCPGrantRejectedError,
    MCPServerUnavailableError,
    MCPSignInUnsupportedError,
)
from agent_core.domain.mcp import (
    MCPAuthorizationMetadata,
    MCPAuthScheme,
    MCPCallResult,
    MCPClientRegistration,
    MCPDiscovery,
    MCPProbeResult,
    MCPRemoteTool,
    MCPServerAccess,
    MCPServerConfig,
    MCPTokenGrant,
    MCPTransport,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import ScriptedToolCall, ScriptedTurn, StopReason, TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.runs import RunStatus
from agent_core.mcp.mapping import map_discovered_tools
from agent_core.mcp.runtime import MCPRuntime
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import tool_context
from tests.gates.test_deferred_tools_adr0123 import _production
from tests.unit.test_config import base_environment

TENANT = "local"
PRINCIPAL_ID = "local-user"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
CALLBACK = "http://127.0.0.1:8000/v1/mcp-oauth/callback"
ENABLED = {
    "AGENT_MCP_SERVERS_ENABLED": "1",
    "MCP_AUTHORIZATION_KEY": KEY,
    "MCP_OAUTH_CALLBACK_URL": CALLBACK,
}
URL = "https://mcp.example.com/mcp"
AUTHORIZATION_SERVER = "https://auth.example.com"
METADATA = MCPAuthorizationMetadata(
    resource=URL,
    issuer="https://auth.example.com/",
    authorization_endpoint=AUTHORIZATION_SERVER + "/authorize",
    token_endpoint=AUTHORIZATION_SERVER + "/token",
    registration_endpoint=AUTHORIZATION_SERVER + "/register",
    scopes=("design:read", "design:write"),
    token_endpoint_auth_methods=("client_secret_post", "none"),
)
SECRET_VALUES = ("client-secret-1", "access-1", "refresh-1")


class FakeGateway:
    """Authored sign-in traffic: what the probe finds and how grants answer."""

    def __init__(
        self,
        *,
        authorization: MCPAuthorizationMetadata | None = METADATA,
        probe_error: Exception | None = None,
        exchange_error: Exception | None = None,
        refresh_error: Exception | None = None,
        expires_in: int | None = 3600,
    ) -> None:
        self.authorization = authorization
        self.probe_error = probe_error
        self.exchange_error = exchange_error
        self.refresh_error = refresh_error
        self.expires_in = expires_in
        self.calls: list[tuple[str, Any]] = []
        self.refreshes = 0

    async def probe(self, url: str) -> MCPProbeResult:
        self.calls.append(("probe", url))
        if self.probe_error is not None:
            raise self.probe_error
        return MCPProbeResult(authorization=self.authorization)

    async def register(
        self, metadata: MCPAuthorizationMetadata, redirect_uri: str
    ) -> MCPClientRegistration:
        self.calls.append(("register", redirect_uri))
        return MCPClientRegistration(
            client_id="client-1", client_secret=SECRET_VALUES[0], auth_method="client_secret_post"
        )

    async def exchange(
        self,
        metadata: MCPAuthorizationMetadata,
        registration: MCPClientRegistration,
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> MCPTokenGrant:
        self.calls.append(("exchange", (code, verifier, redirect_uri, registration)))
        if self.exchange_error is not None:
            raise self.exchange_error
        return MCPTokenGrant(
            access_token="access-1", refresh_token="refresh-1", expires_in=self.expires_in
        )

    async def refresh(
        self,
        metadata: MCPAuthorizationMetadata,
        registration: MCPClientRegistration,
        *,
        refresh_token: str,
    ) -> MCPTokenGrant:
        self.calls.append(("refresh", refresh_token))
        if self.refresh_error is not None:
            raise self.refresh_error
        self.refreshes += 1
        number = self.refreshes + 1
        return MCPTokenGrant(
            access_token=f"access-{number}",
            refresh_token=f"refresh-{number}",
            expires_in=self.expires_in,
        )


def _principal(*scopes: str) -> Principal:
    return Principal(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        roles={"user"},
        scopes=set(scopes or ("settings.read", "settings.write")),
    )


@asynccontextmanager
async def _composition(
    *scopes: str,
    environment: dict[str, str] | None = None,
    gateway: FakeGateway | None = None,
    **build_kwargs: Any,
) -> AsyncIterator[Composition]:
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            **(ENABLED if environment is None else environment),
        }
    )
    async with build(
        settings=settings,
        storage="memory",
        sequential_ids=True,
        principal=_principal(*scopes),
        clock=build_kwargs.pop("clock", FixedClock(NOW)),
        mcp_oauth_gateway=gateway or FakeGateway(),
        **build_kwargs,
    ) as composition:
        yield composition


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


def _add(server_id: str = "docs", url: str = URL, access: str = "changes") -> dict[str, str]:
    return {"server_id": server_id, "url": url, "access": access}


async def _stored(composition: Composition, server_id: str = "docs") -> MCPServerConfig | None:
    async with composition.uow_factory() as uow:
        stored: MCPServerConfig | None = await uow.mcp_servers.get(TENANT, server_id)
        return stored


async def _signed_in(client: httpx.AsyncClient, server_id: str = "docs") -> None:
    started = await client.post(f"/v1/mcp-servers/{server_id}/sign-in")
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
    callback = await client.get("/v1/mcp-oauth/callback", params={"state": state, "code": "c"})
    assert callback.headers["Location"].endswith("outcome=signed_in")


# The Settings routes


async def test_enabled_settings_list_mcp_servers() -> None:
    async with _composition() as composition, _client(composition) as client:
        response = await client.get("/v1/mcp-servers")
    assert response.status_code == 200
    assert response.json() == {"servers": []}
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/mcp-servers"),
        ("POST", "/v1/mcp-servers"),
        ("PATCH", "/v1/mcp-servers/docs"),
        ("DELETE", "/v1/mcp-servers/docs"),
        ("POST", "/v1/mcp-servers/docs/sign-in"),
        ("DELETE", "/v1/mcp-servers/docs/sign-in"),
        ("GET", "/v1/mcp-oauth/callback"),
    ],
)
async def test_the_disabled_feature_mounts_no_route(method: str, path: str) -> None:
    async with _composition(environment={}) as composition, _client(composition) as client:
        response = await client.request(method, path, json=_add())
    assert response.status_code == 404
    assert composition.services.mcp_servers is None


async def test_an_open_server_is_ready_and_an_identical_add_is_a_replay() -> None:
    gateway = FakeGateway(authorization=None)
    async with _composition(gateway=gateway) as composition, _client(composition) as client:
        created = await client.post("/v1/mcp-servers", json=_add())
        replayed = await client.post("/v1/mcp-servers", json=_add())
        listed = await client.get("/v1/mcp-servers")
        stored = await _stored(composition)
    assert created.status_code == 201
    assert created.json() == {
        "server_id": "docs",
        "url": URL,
        "managed_by": "owner",
        "access": "changes",
        "authentication": "none",
        "status": "ready",
        "sign_in_host": None,
        "signed_in_at": None,
    }
    assert replayed.status_code == 200
    assert replayed.json() == created.json()
    assert listed.json() == {"servers": [created.json()]}
    assert gateway.calls == [("probe", URL)]
    assert stored is not None
    assert stored.owner_managed and not stored.operator_configured
    assert stored.transport is MCPTransport.HTTP
    assert stored.auth_scheme is MCPAuthScheme.NONE
    assert stored.required_scopes == frozenset()
    # Unclassified by the owner means the strictest classification.
    assert (stored.side_effect, stored.risk, stored.idempotency) == (
        SideEffectClass.EXTERNAL_WRITE,
        RiskLevel.HIGH,
        IdempotencyClass.NON_IDEMPOTENT,
    )


async def test_a_server_that_wants_sign_in_records_its_authorization_server() -> None:
    async with _composition() as composition, _client(composition) as client:
        created = await client.post("/v1/mcp-servers", json=_add(access="read_only"))
        stored = await _stored(composition)
        async with composition.uow_factory() as uow:
            authorization = await uow.mcp_servers.get_authorization(TENANT, "docs")
    assert created.status_code == 201
    body = created.json()
    assert (body["authentication"], body["status"], body["sign_in_host"]) == (
        "sign_in",
        "sign_in_required",
        "auth.example.com",
    )
    assert body["access"] == "read_only"
    assert stored is not None and stored.auth_scheme is MCPAuthScheme.OAUTH2_AUTHORIZATION
    assert stored.credential_ref is None
    assert (stored.side_effect, stored.risk, stored.idempotency) == (
        SideEffectClass.NETWORK_READ,
        RiskLevel.LOW,
        IdempotencyClass.READ_ONLY,
    )
    assert authorization is not None and authorization.metadata == METADATA
    assert not authorization.signed_in


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"server_id": "docs"},
        {"url": URL},
        _add(server_id="Docs"),
        _add(server_id="1docs"),
        _add(server_id="d" * 33),
        _add(server_id="docs-two"),
        _add(url="http://mcp.example.com/mcp"),
        _add(url="https://localhost/mcp"),
        _add(url="https://10.0.0.8/mcp"),
        _add(url="https://[::1]/mcp"),
        _add(url="https://tools.internal/mcp"),
        _add(url="https://mcp.example.com:8443/mcp"),
        # Credentials in the URL, assembled so no literal carries them.
        _add(url="https://" + ":".join(("user", "word")) + "@mcp.example.com/mcp"),
        _add(url="https://mcp.example.com/mcp#fragment"),
        _add(url="mcp.example.com/mcp"),
        _add(access="everything"),
        {**_add(), "tenant_id": "other"},
    ],
)
async def test_an_invalid_add_is_malformed_and_stores_nothing(body: dict[str, Any]) -> None:
    gateway = FakeGateway()
    async with _composition(gateway=gateway) as composition, _client(composition) as client:
        response = await client.post("/v1/mcp-servers", json=body)
        listed = await client.get("/v1/mcp-servers")
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "malformed_request"
    assert listed.json() == {"servers": []}
    assert gateway.calls == []


async def test_reserved_taken_and_operator_names_conflict() -> None:
    operator = MCPServerConfig(
        tenant_id=TENANT,
        server_id="docs",
        transport=MCPTransport.STDIO,
        endpoint="/fixture/docs",
        operator_configured=True,
        side_effect=SideEffectClass.NETWORK_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
    )
    script = ScriptedMCPServer(name="docs", discovery=MCPDiscovery())
    async with (
        _composition(mcp_servers=(operator,), mcp_scripts={"docs": script}) as composition,
        _client(composition) as client,
    ):
        reserved = await client.post("/v1/mcp-servers", json=_add(server_id="gmail_extra"))
        operator_name = await client.post("/v1/mcp-servers", json=_add())
        patched = await client.patch("/v1/mcp-servers/docs", json={"access": "read_only"})
        removed = await client.delete("/v1/mcp-servers/docs")
        sign_in = await client.post("/v1/mcp-servers/docs/sign-in")
        await client.post("/v1/mcp-servers", json=_add(server_id="tools"))
        taken = await client.post(
            "/v1/mcp-servers", json=_add(server_id="tools", url="https://other.example.com/mcp")
        )
        listed = await client.get("/v1/mcp-servers")
    for response in (reserved, operator_name, patched, removed, sign_in, taken):
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "conflict"
    assert listed.json()["servers"][0] == {
        "server_id": "docs",
        "url": None,
        "managed_by": "operator",
        "access": "read_only",
        "authentication": "operator",
        "status": "ready",
        "sign_in_host": None,
        "signed_in_at": None,
    }


async def test_a_seventeenth_server_is_refused() -> None:
    async with (
        _composition(gateway=FakeGateway(authorization=None)) as composition,
        _client(composition) as client,
    ):
        for index in range(16):
            created = await client.post(
                "/v1/mcp-servers",
                json=_add(server_id=f"s{index}", url=f"https://s{index}.example.com/mcp"),
            )
            assert created.status_code == 201
        refused = await client.post(
            "/v1/mcp-servers", json=_add(server_id="s16", url="https://s16.example.com/mcp")
        )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (MCPServerUnavailableError(), 502, "mcp_server_unavailable"),
        (MCPSignInUnsupportedError(), 422, "mcp_sign_in_unsupported"),
    ],
)
async def test_a_server_veetbot_cannot_use_is_refused_with_its_own_code(
    error: Exception, status: int, code: str
) -> None:
    gateway = FakeGateway(probe_error=error)
    async with _composition(gateway=gateway) as composition, _client(composition) as client:
        response = await client.post("/v1/mcp-servers", json=_add())
        listed = await client.get("/v1/mcp-servers")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert listed.json() == {"servers": []}


async def test_reads_and_writes_need_their_own_settings_scope() -> None:
    async with _composition("settings.read") as composition, _client(composition) as client:
        assert (await client.get("/v1/mcp-servers")).status_code == 200
        for method, path in (
            ("POST", "/v1/mcp-servers"),
            ("PATCH", "/v1/mcp-servers/docs"),
            ("DELETE", "/v1/mcp-servers/docs"),
            ("POST", "/v1/mcp-servers/docs/sign-in"),
            ("DELETE", "/v1/mcp-servers/docs/sign-in"),
        ):
            response = await client.request(method, path, json=_add())
            assert response.status_code == 403, (method, path)
    async with _composition("settings.write") as composition, _client(composition) as client:
        assert (await client.get("/v1/mcp-servers")).status_code == 403


async def test_access_changes_reclassify_and_removal_is_idempotent() -> None:
    async with (
        _composition(gateway=FakeGateway(authorization=None)) as composition,
        _client(composition) as client,
    ):
        await client.post("/v1/mcp-servers", json=_add())
        patched = await client.patch("/v1/mcp-servers/docs", json={"access": "read_only"})
        stored = await _stored(composition)
        unknown = await client.patch("/v1/mcp-servers/missing", json={"access": "read_only"})
        invalid = await client.patch("/v1/mcp-servers/docs", json={"access": "all"})
        first = await client.delete("/v1/mcp-servers/docs")
        second = await client.delete("/v1/mcp-servers/docs")
        listed = await client.get("/v1/mcp-servers")
        async with composition.uow_factory() as uow:
            events = await uow.process_events.list("settings.mcp_servers.removed")
    assert patched.status_code == 200 and patched.json()["access"] == "read_only"
    assert stored is not None and stored.side_effect is SideEffectClass.NETWORK_READ
    assert unknown.status_code == 404
    assert invalid.status_code == 400
    assert (first.status_code, second.status_code) == (204, 204)
    assert listed.json() == {"servers": []}
    assert len(events) == 1 and events[0].payload == {
        "tenant_id": TENANT,
        "server_id": "docs",
        "access": "read_only",
    }


# Signing in


async def test_sign_in_registers_once_and_the_callback_stores_sealed_tokens() -> None:
    gateway = FakeGateway()
    async with _composition(gateway=gateway) as composition, _client(composition) as client:
        await client.post("/v1/mcp-servers", json=_add())
        started = await client.post("/v1/mcp-servers/docs/sign-in")
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
        callback = await client.get(
            "/v1/mcp-oauth/callback", params={"state": query["state"][0], "code": "the-code"}
        )
        replay = await client.get(
            "/v1/mcp-oauth/callback", params={"state": query["state"][0], "code": "the-code"}
        )
        listed = await client.get("/v1/mcp-servers")
        again = await client.post("/v1/mcp-servers/docs/sign-in")
        async with composition.uow_factory() as uow:
            authorization = await uow.mcp_servers.get_authorization(TENANT, "docs")
            events = await uow.process_events.list("settings.mcp_servers.signed_in")
    assert started.status_code == 200
    assert started.json()["callback_scheme"] == "veetbot"
    assert started.json()["expires_at"] == (NOW + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    assert urlsplit(started.json()["authorization_url"]).netloc == "auth.example.com"
    assert {name: values[0] for name, values in query.items() if name != "state"} == {
        "response_type": "code",
        "client_id": "client-1",
        "redirect_uri": CALLBACK,
        "code_challenge": query["code_challenge"][0],
        "code_challenge_method": "S256",
        "resource": URL,
        "scope": "design:read design:write",
    }
    assert callback.status_code == 303
    assert callback.headers["Location"] == "veetbot://mcp-sign-in?server_id=docs&outcome=signed_in"
    assert callback.headers["Cache-Control"] == "private, no-store"
    assert "the-code" not in callback.text
    assert replay.headers["Location"] == "veetbot://mcp-sign-in?outcome=expired"
    server = listed.json()["servers"][0]
    assert server["status"] == "ready"
    assert server["signed_in_at"] is not None
    assert again.status_code == 200
    # The registration is kept for the same callback, so a second sign-in reuses it.
    assert [call[0] for call in gateway.calls] == ["probe", "register", "exchange"]
    exchanged = gateway.calls[2][1]
    assert exchanged[0] == "the-code" and exchanged[2] == CALLBACK
    assert exchanged[3].client_secret == "client-secret-1"
    assert authorization is not None and authorization.signed_in
    sealed = authorization.model_dump_json()
    assert all(value not in sealed for value in SECRET_VALUES)
    assert len(events) == 1 and events[0].payload == {"tenant_id": TENANT, "server_id": "docs"}


@pytest.mark.parametrize("case", ["denied", "rejected", "expired", "unknown"])
async def test_a_failed_callback_signs_nothing_in(case: str) -> None:
    clock = FixedClock(NOW)
    gateway = FakeGateway(exchange_error=MCPGrantRejectedError() if case == "rejected" else None)
    async with (
        _composition(gateway=gateway, clock=clock) as composition,
        _client(composition) as client,
    ):
        await client.post("/v1/mcp-servers", json=_add())
        started = await client.post("/v1/mcp-servers/docs/sign-in")
        state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
        params: dict[str, str] = {"state": state, "code": "c"}
        if case == "denied":
            params = {"state": state, "error": "access_denied"}
        elif case == "expired":
            clock.advance(timedelta(minutes=11))
        elif case == "unknown":
            params = {"state": "not-a-state", "code": "c"}
        callback = await client.get("/v1/mcp-oauth/callback", params=params)
        listed = await client.get("/v1/mcp-servers")
    expected = {
        "denied": "veetbot://mcp-sign-in?server_id=docs&outcome=failed",
        "rejected": "veetbot://mcp-sign-in?server_id=docs&outcome=failed",
        "expired": "veetbot://mcp-sign-in?server_id=docs&outcome=expired",
        "unknown": "veetbot://mcp-sign-in?outcome=expired",
    }[case]
    assert callback.status_code == 303
    assert callback.headers["Location"] == expected
    assert "access_denied" not in callback.text
    assert listed.json()["servers"][0]["status"] == "sign_in_required"


async def test_signing_out_forgets_the_tokens_and_an_open_server_needs_no_sign_in() -> None:
    async with _composition() as composition, _client(composition) as client:
        await client.post("/v1/mcp-servers", json=_add())
        await _signed_in(client)
        signed_out = await client.delete("/v1/mcp-servers/docs/sign-in")
        listed = await client.get("/v1/mcp-servers")
        async with composition.uow_factory() as uow:
            authorization = await uow.mcp_servers.get_authorization(TENANT, "docs")
    assert signed_out.status_code == 204
    assert listed.json()["servers"][0]["status"] == "sign_in_required"
    assert authorization is not None and not authorization.signed_in
    assert authorization.client_id == "client-1"
    async with (
        _composition(gateway=FakeGateway(authorization=None)) as composition,
        _client(composition) as client,
    ):
        await client.post("/v1/mcp-servers", json=_add())
        refused = await client.post("/v1/mcp-servers/docs/sign-in")
    assert refused.status_code == 409


# The runtime's tokens


async def test_tokens_refresh_near_expiry_and_a_rejected_refresh_signs_out() -> None:
    clock = FixedClock(NOW)
    gateway = FakeGateway(expires_in=600)
    async with (
        _composition(gateway=gateway, clock=clock) as composition,
        _client(composition) as client,
    ):
        service = composition.services.mcp_servers
        assert service is not None
        await client.post("/v1/mcp-servers", json=_add())
        await _signed_in(client)
        fresh = await service.access_token(TENANT, "docs")
        clock.advance(timedelta(seconds=541))
        refreshed = await service.access_token(TENANT, "docs")
        forced = await service.access_token(TENANT, "docs", force_refresh=True)
        gateway.refresh_error = MCPGrantRejectedError()
        with pytest.raises(PermissionError):
            await service.access_token(TENANT, "docs", force_refresh=True)
        listed = await client.get("/v1/mcp-servers")
        with pytest.raises(PermissionError):
            await service.access_token(TENANT, "docs")
    assert fresh.reveal() == "access-1"
    assert refreshed.reveal() == "access-2"
    assert forced.reveal() == "access-3"
    # Each refresh spends the rotated token the previous one stored.
    assert [call[1] for call in gateway.calls if call[0] == "refresh"] == [
        "refresh-1",
        "refresh-2",
        "refresh-3",
    ]
    assert listed.json()["servers"][0]["status"] == "sign_in_required"


def _remote(name: str, description: str = "Read a design.") -> MCPRemoteTool:
    return MCPRemoteTool(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    )


async def test_a_refused_call_forces_one_refresh_and_retries_a_read() -> None:
    script = ScriptedMCPServer(
        name="docs",
        discovery=MCPDiscovery(tools=(_remote("get_design"),)),
        responses=(
            ScriptedMCPResponse(name="get_design", outcome="unauthorized"),
            ScriptedMCPResponse(name="get_design", result=MCPCallResult(content=("design",))),
        ),
    )
    factory = ScriptedMCPClientFactory({"docs": script})
    gateway = FakeGateway()
    async with (
        _composition(gateway=gateway, mcp_client_factory=factory) as composition,
        _client(composition) as client,
    ):
        await client.post("/v1/mcp-servers", json=_add(access="read_only"))
        await _signed_in(client)
        session_id = await composition.sessions.create()
        await composition.mcp.prepare(session_id, composition.principal)
        stored = await _stored(composition)
        assert stored is not None
        spec = map_discovered_tools(stored, script.discovery.tools).accepted[0].spec
        context = replace(tool_context(), session_id=session_id, tenant_id=TENANT)
        result = await composition.mcp.call_tool(context, spec, "get_design", {"id": "a"})
    assert result.ok and isinstance(result.content[0], TextPart)
    assert result.content[0].text == "design"
    assert [call[0] for call in gateway.calls].count("refresh") == 1
    assert factory.created[0].reauthentication_count == 1


async def test_a_signed_out_server_offers_no_tools_and_the_rest_still_load() -> None:
    script = ScriptedMCPServer(name="docs", discovery=MCPDiscovery(tools=(_remote("get"),)))
    factory = ScriptedMCPClientFactory({"docs": script})
    async with (
        _composition(mcp_client_factory=factory) as composition,
        _client(composition) as client,
    ):
        await client.post("/v1/mcp-servers", json=_add())
        session_id = await composition.sessions.create()
        await composition.mcp.prepare(session_id, composition.principal)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
    disconnected = [
        event.payload for event in events if event.event_type == "mcp.server.disconnected"
    ]
    assert disconnected == [{"server_id": "docs", "reason_code": "tool.auth_failed"}]
    assert factory.created == []


async def test_turning_the_feature_off_ignores_owner_rows() -> None:
    script = ScriptedMCPServer(name="docs", discovery=MCPDiscovery(tools=(_remote("get"),)))
    factory = ScriptedMCPClientFactory({"docs": script})
    async with _composition(gateway=FakeGateway(authorization=None)) as composition:
        service = composition.services.mcp_servers
        assert service is not None
        await service.add(composition.principal, "docs", URL, MCPServerAccess.READ_ONLY)
        # A runtime with the feature off, over the same rows, starts none of them.
        disabled = MCPRuntime(
            composition.uow_factory,
            StaticToolRegistry(),
            factory,
            MappingCredentialResolver({}),
            composition.clock,
            composition.ids,
        )
        session_id = await composition.sessions.create()
        await disabled.prepare(session_id, composition.principal)
        await disabled.close()
    assert factory.created == []


# The owner index


MAGICPATH_TOOLS = (
    "add_project_image",
    "arrange_canvas_shapes",
    "cancel_component_code_session",
    "create_canvas_shapes",
    "create_project",
    "create_skill",
    "delete_canvas_shapes",
    "delete_skill",
    "get_active_projects",
    "get_api_quota",
    "get_canvas_context",
    "get_component_build_status",
    "get_component_code_context",
    "get_component_install_plan",
    "get_context",
    "get_project",
    "get_project_export",
    "get_selection",
    "get_share_url",
    "get_skill",
    "get_theme",
    "group_canvas_shapes",
    "import_skill",
    "inspect_component",
    "list_components",
    "list_project_images",
    "list_projects",
    "list_skills",
    "list_team_members",
    "list_teams",
    "list_themes",
    "order_canvas_shapes",
    "prepare_asset_upload",
    "recover_component_code_session",
    "search_components",
    "start_component_code_session",
    "style_canvas_shapes",
    "submit_component_code",
    "transform_canvas_shapes",
    "update_skill",
    "view_canvas_image",
    "view_component",
)


MAGICPATH_URL = "https://api.magicpath.ai/mcp"


def _magicpath(*responses: ScriptedMCPResponse) -> ScriptedMCPServer:
    return ScriptedMCPServer(
        name="magicpath",
        discovery=MCPDiscovery(
            tools=tuple(
                _remote(name, f"MagicPath {name.replace('_', ' ')} for the canvas.")
                for name in MAGICPATH_TOOLS
            )
        ),
        responses=responses,
    )


def _magicpath_gateway() -> FakeGateway:
    return FakeGateway(authorization=METADATA.model_copy(update={"resource": MAGICPATH_URL}))


async def _add_and_sign_in(app: Composition, access: MCPServerAccess) -> None:
    """What the owner does in Settings: add the pasted server, then sign in."""
    service = app.services.mcp_servers
    assert service is not None
    await service.add(app.principal, "magicpath", MAGICPATH_URL, access)
    started = await service.begin_sign_in(app.principal, "magicpath")
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    assert await service.complete_sign_in(state, "code", None) == ("magicpath", "signed_in")


async def _owner_plan(tmp_path: Path, *, with_owner_server: bool) -> Any:
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment=ENABLED,
        extra_scripts={"magicpath": _magicpath()},
        mcp_oauth_gateway=_magicpath_gateway(),
    )
    async with context as app:
        if with_owner_server:
            await _add_and_sign_in(app, MCPServerAccess.CHANGES)
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("What can you do for me?", session_id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        plan = await app.executor._context_planner.current(session_id)
    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    assert plan is not None
    return plan


async def test_an_owner_server_fills_its_own_index_and_moves_no_existing_tool(
    tmp_path: Path,
) -> None:
    baseline = await _owner_plan(tmp_path / "baseline", with_owner_server=False)
    owner = await _owner_plan(tmp_path / "owner", with_owner_server=True)
    owner_names = {f"mcp.magicpath.{name}" for name in MAGICPATH_TOOLS}
    assert owner.tool_names == baseline.tool_names
    assert not owner_names & set(owner.tool_names)
    added = set(owner.deferred_tool_names) - set(baseline.deferred_tool_names)
    assert added <= owner_names
    assert set(baseline.deferred_tool_names) <= set(owner.deferred_tool_names)
    # Forty-two tools exceed the forty-item index: the rest are recorded, not dropped silently.
    skipped = set(owner.skipped_tool_names) - set(baseline.skipped_tool_names)
    assert added | skipped == owner_names
    assert len(added) <= 40 and skipped
    # Reads take the owner index first.
    assert {"mcp.magicpath.get_selection", "mcp.magicpath.list_projects"} <= added


@pytest.mark.parametrize("access", [MCPServerAccess.READ_ONLY, MCPServerAccess.CHANGES])
async def test_chat_reaches_an_owner_tool_through_tool_call_under_its_access(
    tmp_path: Path, access: MCPServerAccess
) -> None:
    """A read-only server runs at once; one that can change things waits for approval."""
    tool = "mcp.magicpath.get_selection"
    factory, context = await _production(
        tmp_path,
        [
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="tool.call", arguments={"name": tool, "arguments": {"id": "frame"}}
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The selection is a frame.", stop_reason=StopReason.END_TURN),
        ],
        extra_environment=ENABLED,
        extra_scripts={
            "magicpath": _magicpath(
                ScriptedMCPResponse(
                    name="get_selection", result=MCPCallResult(content=("one frame selected",))
                )
            )
        },
        mcp_oauth_gateway=_magicpath_gateway(),
    )
    async with context as app:
        await _add_and_sign_in(app, access)
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("What did I select in MagicPath?", session_id)
        if access is MCPServerAccess.CHANGES:
            [approval] = await app.approvals.list_pending(run_id=run_id)
            assert approval.tool_name == tool
            assert sum(client.call_count for client in factory.created) == 0
            await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        assert await app.approvals.list_pending(run_id=run_id) == []
    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    assert terminal.final_message == "The selection is a frame."
    [client] = [created for created in factory.created if created.call_count]
    assert client.call_count == 1
    # The sign-in's access token is the credential the connection used.
    assert client._credential is not None and client._credential.reveal() == "access-1"
