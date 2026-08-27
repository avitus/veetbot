"""Fail-closed MCP configuration and child-environment construction."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPAuthScheme, MCPServerConfig, MCPTransport
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.execution.environment import TIER_ZERO_NAMES, build_sandbox_environment
from agent_core.policy.scopes import validate_required_scopes

type DestinationAllowed = Callable[[str], bool]

_EMAIL_CLASSIFICATIONS = {
    "gmail_read": (
        SideEffectClass.NETWORK_READ,
        RiskLevel.LOW,
        IdempotencyClass.READ_ONLY,
    ),
    "gmail_write": (
        SideEffectClass.EXTERNAL_WRITE,
        RiskLevel.MEDIUM,
        IdempotencyClass.NON_IDEMPOTENT,
    ),
    "gmail_send": (
        SideEffectClass.EXTERNAL_MESSAGE,
        RiskLevel.HIGH,
        IdempotencyClass.NON_IDEMPOTENT,
    ),
}


def email_server_configs(
    tenant_id: str,
    *,
    enabled: bool = True,
) -> tuple[MCPServerConfig, ...]:
    """Return the operator-owned first-party email server rows when enabled."""

    if not enabled:
        return ()
    rows: list[MCPServerConfig] = []
    for server_id, (side_effect, risk, idempotency) in _EMAIL_CLASSIFICATIONS.items():
        mode = server_id.removeprefix("gmail_")
        rows.append(
            MCPServerConfig(
                tenant_id=tenant_id,
                server_id=server_id,
                transport=MCPTransport.STDIO,
                endpoint=shlex.join((sys.executable, "-m", "gmail_mcp", "--mode", mode)),
                operator_configured=True,
                auth_scheme=MCPAuthScheme.ENV,
                auth_name="GMAIL_MCP_CREDENTIAL",
                credential_ref=server_id,
                side_effect=side_effect,
                risk=risk,
                idempotency=idempotency,
                required_scopes=frozenset({f"mcp.{server_id}.use"}),
            )
        )
    return tuple(rows)


def validate_mcp_config(
    config: MCPServerConfig,
    *,
    destination_allowed: DestinationAllowed,
) -> MCPServerConfig:
    """Validate policy-dependent rules before a server configuration is stored."""

    if config.auth_scheme is MCPAuthScheme.ENV and config.auth_name in TIER_ZERO_NAMES:
        raise ValueError("MCP env authentication may not name a tier-0 variable")
    if config.transport is MCPTransport.HTTP and config.auth_scheme in {
        MCPAuthScheme.BEARER,
        MCPAuthScheme.HEADER,
        MCPAuthScheme.OAUTH2_CLIENT,
    }:
        endpoint = urlsplit(config.endpoint)
        if endpoint.scheme != "https" or endpoint.hostname is None:
            raise ValueError("authenticated MCP endpoints require HTTPS")
    if config.auth_scheme is MCPAuthScheme.OAUTH2_CLIENT:
        token_endpoint = urlsplit(config.token_endpoint or "")
        if token_endpoint.scheme != "https" or token_endpoint.hostname is None:
            raise ValueError("MCP OAuth token endpoints require HTTPS")
    validate_required_scopes(set(config.required_scopes), mcp_server_id=config.server_id)
    if config.server_id in _EMAIL_CLASSIFICATIONS and config.required_scopes != {
        f"mcp.{config.server_id}.use"
    }:
        raise ValueError("a Gmail MCP server requires exactly its use scope")
    if config.transport is MCPTransport.HTTP and not destination_allowed(config.endpoint):
        raise ValueError("MCP endpoint is not permitted by the egress policy")
    if (
        config.auth_scheme is MCPAuthScheme.OAUTH2_CLIENT
        and config.token_endpoint is not None
        and not destination_allowed(config.token_endpoint)
    ):
        raise ValueError("MCP token endpoint is not permitted by the egress policy")
    return config


def build_stdio_environment(
    config: MCPServerConfig,
    credential: SecretValue | None,
    *,
    synthesized: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment without consulting the worker environment."""

    if config.transport is not MCPTransport.STDIO:
        raise ValueError("a stdio environment requires the stdio transport")
    environment = build_sandbox_environment({})
    supplied = dict(synthesized or {})
    overridden = sorted(set(supplied) & TIER_ZERO_NAMES)
    if overridden:
        raise ValueError(f"synthesized environment may not name tier-0 variables: {overridden}")
    environment.update(supplied)
    if config.auth_scheme is MCPAuthScheme.ENV:
        if config.auth_name is None or credential is None:
            raise ValueError("stdio env authentication requires a name and credential")
        if config.auth_name in TIER_ZERO_NAMES:
            raise ValueError("MCP env authentication may not name a tier-0 variable")
        environment[config.auth_name] = credential.reveal()
    elif credential is not None:
        raise ValueError("credential supplied to an unauthenticated stdio server")
    return environment
