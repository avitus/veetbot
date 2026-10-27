"""ADR-0255 units: settings, the row shape, sealing, sign-in HTTP, transport and policy."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from pydantic import ValidationError

from agent_core.adapters.mcp.sdk import SDKMCPClient, SDKMCPClientFactory
from agent_core.config import ConfigurationError, load_settings
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import (
    OWNER_ACCESS_CLASSIFICATIONS,
    MCPAuthScheme,
    MCPServerAccess,
    MCPServerConfig,
    MCPTransport,
    server_access,
)
from agent_core.domain.policies import PolicyDecisionType, RiskLevel
from agent_core.execution import proxy
from agent_core.mcp.configuration import validate_mcp_config
from tests.gates.test_email_unsubscribe_transport_m31 import (
    PUBLIC_ADDRESS,
    Decision,
    _connect,
    _last,
    _reader,
)
from tests.gates.test_proxy_m6 import _Writer
from tests.unit.test_config import base_environment

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
CALLBACK = "https://api.veetbot.example/v1/mcp-oauth/callback"
MCP_URL = "https://api.magicpath.ai/mcp"
SCHEME = "Bearer"


def _owner(**overrides: Any) -> dict[str, Any]:
    return {
        "tenant_id": "local",
        "server_id": "magicpath",
        "transport": MCPTransport.HTTP,
        "endpoint": MCP_URL,
        "owner_managed": True,
        "auth_scheme": MCPAuthScheme.OAUTH2_AUTHORIZATION,
        **overrides,
    }


# Settings


def _settings(**environment: str) -> Any:
    return load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake", **environment})


def test_the_feature_is_off_by_default_and_needs_its_key_and_callback() -> None:
    settings = _settings()
    assert settings.mcp_servers_enabled is False
    assert settings.mcp_authorization_key is None and settings.mcp_oauth_callback_url is None
    enabled = _settings(
        AGENT_MCP_SERVERS_ENABLED="1", MCP_AUTHORIZATION_KEY=KEY, MCP_OAUTH_CALLBACK_URL=CALLBACK
    )
    assert enabled.mcp_servers_enabled is True
    assert enabled.mcp_authorization_key is not None
    assert enabled.mcp_authorization_key.get_secret_value() == KEY
    assert enabled.mcp_oauth_callback_url == CALLBACK
    assert _settings(
        AGENT_MCP_SERVERS_ENABLED="1",
        MCP_AUTHORIZATION_KEY=KEY.rstrip("="),
        MCP_OAUTH_CALLBACK_URL="http://localhost:8000/v1/mcp-oauth/callback",
    ).mcp_servers_enabled


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"AGENT_MCP_SERVERS_ENABLED": "1"}, "MCP_AUTHORIZATION_KEY"),
        (
            {"AGENT_MCP_SERVERS_ENABLED": "1", "MCP_AUTHORIZATION_KEY": KEY},
            "MCP_OAUTH_CALLBACK_URL",
        ),
        (
            {
                "AGENT_MCP_SERVERS_ENABLED": "1",
                "MCP_AUTHORIZATION_KEY": base64.urlsafe_b64encode(b"short").decode(),
                "MCP_OAUTH_CALLBACK_URL": CALLBACK,
            },
            "32 bytes",
        ),
        (
            {
                "AGENT_MCP_SERVERS_ENABLED": "1",
                "MCP_AUTHORIZATION_KEY": "not base64 !",
                "MCP_OAUTH_CALLBACK_URL": CALLBACK,
            },
            "MCP_AUTHORIZATION_KEY",
        ),
        (
            {
                "AGENT_MCP_SERVERS_ENABLED": "1",
                "MCP_AUTHORIZATION_KEY": KEY,
                "MCP_OAUTH_CALLBACK_URL": "http://api.veetbot.example/v1/mcp-oauth/callback",
            },
            "MCP_OAUTH_CALLBACK_URL",
        ),
        (
            {
                "AGENT_MCP_SERVERS_ENABLED": "1",
                "MCP_AUTHORIZATION_KEY": KEY,
                "MCP_OAUTH_CALLBACK_URL": "https://api.veetbot.example/callback",
            },
            "MCP_OAUTH_CALLBACK_URL",
        ),
        ({"MCP_AUTHORIZATION_KEY": KEY}, "AGENT_MCP_SERVERS_ENABLED"),
        ({"AGENT_MCP_SERVERS_ENABLED": "yes"}, "AGENT_MCP_SERVERS_ENABLED"),
    ],
)
def test_incomplete_settings_fail_closed(environment: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _settings(**environment)


# The row


def test_an_owner_row_is_tenant_public_https_with_no_inline_credential() -> None:
    config = MCPServerConfig.model_validate(_owner())
    assert config.credential_ref is None
    validate_mcp_config(config, destination_allowed=lambda _url: False)
    assert MCPServerConfig.model_validate(_owner(auth_scheme=MCPAuthScheme.NONE)).owner_managed
    for invalid in (
        _owner(transport=MCPTransport.STDIO, operator_configured=True),
        _owner(operator_configured=True),
        _owner(auth_scheme=MCPAuthScheme.BEARER, credential_ref="token"),
        _owner(credential_ref="token"),
        _owner(endpoint="https://api.magicpath.ai:8443/mcp"),
        _owner(endpoint="https://127.0.0.1/mcp"),
        _owner(endpoint="http://api.magicpath.ai/mcp"),
        _owner(endpoint="https://design.lan/mcp"),
        # The sign-in scheme belongs to owner-added rows only.
        _owner(owner_managed=False),
    ):
        with pytest.raises(ValidationError):
            MCPServerConfig.model_validate(invalid)


def test_access_maps_both_ways() -> None:
    for access, (side_effect, risk, idempotency) in OWNER_ACCESS_CLASSIFICATIONS.items():
        config = MCPServerConfig.model_validate(
            _owner(side_effect=side_effect, risk=risk, idempotency=idempotency)
        )
        assert server_access(config) is access
    assert server_access(MCPServerConfig.model_validate(_owner())) is MCPServerAccess.CHANGES


# The MCP client


async def test_a_sign_in_token_is_a_bearer_and_owner_rows_use_the_owner_transport() -> None:
    config = MCPServerConfig.model_validate(_owner())
    client = SDKMCPClient(config, SecretValue("access-1"), {})
    assert await client._headers() == {"Authorization": f"{SCHEME} access-1"}
    factory = SDKMCPClientFactory(
        http_proxy_url="http://127.0.0.1:1", owner_proxy_url="http://127.0.0.1:2"
    )
    assert factory(config, None, {})._http_proxy_url == "http://127.0.0.1:2"
    tenant = MCPServerConfig(
        tenant_id="local",
        server_id="tenant",
        transport=MCPTransport.HTTP,
        endpoint="https://allowed.test/mcp",
    )
    assert factory(tenant, None, {})._http_proxy_url == "http://127.0.0.1:1"
    from agent_core.domain.errors import MCPTransportError

    with pytest.raises(MCPTransportError):
        SDKMCPClientFactory(http_proxy_url="http://127.0.0.1:1")(config, None, {})


# The transport


MCP_POLICY: tuple[str, tuple[tuple[str, frozenset[int]], ...]] = ("mcp_https", ())


async def test_owner_mcp_egress_is_public_https_only(monkeypatch: pytest.MonkeyPatch) -> None:
    audited: list[Decision] = []
    monkeypatch.setattr(
        proxy,
        "_log",
        lambda host, port, addresses, reason: audited.append((host, port, addresses, reason)),
    )
    addresses: tuple[str, ...] = ()

    async def resolves(_host: str, _port: int) -> tuple[str, ...]:
        return addresses

    dials: list[tuple[str, int]] = []

    async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, Any]:
        dials.append((host, port))
        upstream = asyncio.StreamReader()
        upstream.feed_eof()
        return upstream, _Writer()

    monkeypatch.setattr(proxy, "_resolved", resolves)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    for target in (
        "api.magicpath.ai:80",
        "api.magicpath.ai:8443",
        "localhost:443",
        "tools.internal:443",
    ):
        writer = _Writer()
        await proxy._handle(_reader(_connect(target)), writer, MCP_POLICY)  # type: ignore[arg-type]
        assert bytes(writer.data).startswith(b"HTTP/1.1 403"), target
        assert _last(audited)[3] == "mcp_destination_denied", target
    addresses = ("10.0.0.7",)
    writer = _Writer()
    await proxy._handle(_reader(_connect("api.magicpath.ai:443")), writer, MCP_POLICY)  # type: ignore[arg-type]
    assert bytes(writer.data).startswith(b"HTTP/1.1 403")
    assert _last(audited)[3] == "private_address"
    addresses = (PUBLIC_ADDRESS,)
    writer = _Writer()
    await proxy._handle(_reader(_connect("api.magicpath.ai:443")), writer, MCP_POLICY)  # type: ignore[arg-type]
    assert bytes(writer.data).startswith(b"HTTP/1.1 200")
    assert dials == [(PUBLIC_ADDRESS, 443)]


async def test_the_owner_transport_binds_its_own_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[Any, str]] = []

    async def start(policy: Any, *, tenant_id: str) -> Any:
        started.append((policy, tenant_id))
        return object()

    monkeypatch.setattr(proxy, "_start_proxy", start)
    await proxy.start_owner_mcp_egress_proxy(tenant_id="local")
    assert started == [(("mcp_https", ()), "local")]


# Policy


@pytest.mark.parametrize(
    ("access", "expected"),
    [
        (MCPServerAccess.READ_ONLY, PolicyDecisionType.ALLOW),
        (MCPServerAccess.CHANGES, PolicyDecisionType.REQUIRE_APPROVAL),
    ],
)
def test_the_owner_access_level_decides_approval(
    access: MCPServerAccess, expected: PolicyDecisionType
) -> None:
    from agent_core.policy.engine import evaluate_deterministic
    from tests.gates.test_email_m18 import _action, _principal, _ruleset, _run

    side_effect, risk, idempotency = OWNER_ACCESS_CLASSIFICATIONS[access]
    action = _action(
        server_id="magicpath", side_effect=side_effect, idempotency=idempotency
    ).model_copy(update={"required_scopes": set(), "risk": risk})
    decision = evaluate_deterministic(action, _principal(), _run(), _ruleset())
    assert decision.decision is expected
    assert risk in {RiskLevel.LOW, RiskLevel.HIGH}
