"""The three operator-tier Gmail MCP server rows (Milestone 18).

The composition root synthesizes these when ``AGENT_EMAIL_ENABLED`` is set.
One server per side-effect class, because the tool system classifies at the
server level; the stdio command runs the first-party ``gmail_mcp`` package
with a non-secret mode flag, and the credential travels only as the one
declared environment variable the broker resolves per server.
"""

from __future__ import annotations

import shlex
import sys

from agent_core.domain.mcp import MCPAuthScheme, MCPServerConfig, MCPTransport
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass

EMAIL_CREDENTIAL_VARIABLE = "GMAIL_MCP_CREDENTIAL"
EMAIL_SERVER_IDS: tuple[str, ...] = ("gmail_read", "gmail_write", "gmail_send")
EMAIL_SCOPES: frozenset[str] = frozenset(f"mcp.{server_id}.use" for server_id in EMAIL_SERVER_IDS)

_CLASSIFICATIONS: dict[str, tuple[SideEffectClass, RiskLevel, IdempotencyClass]] = {
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
    tenant_id: str, *, python_executable: str | None = None
) -> tuple[MCPServerConfig, ...]:
    """The three rows, classification declared here and nowhere else."""

    interpreter = python_executable or sys.executable
    rows = []
    for server_id in EMAIL_SERVER_IDS:
        side_effect, risk, idempotency = _CLASSIFICATIONS[server_id]
        mode = server_id.removeprefix("gmail_")
        rows.append(
            MCPServerConfig(
                tenant_id=tenant_id,
                server_id=server_id,
                transport=MCPTransport.STDIO,
                endpoint=f"{shlex.quote(interpreter)} -m gmail_mcp --mode {mode}",
                operator_configured=True,
                auth_scheme=MCPAuthScheme.ENV,
                auth_name=EMAIL_CREDENTIAL_VARIABLE,
                credential_ref=server_id,
                side_effect=side_effect,
                risk=risk,
                idempotency=idempotency,
                required_scopes=frozenset({f"mcp.{server_id}.use"}),
            )
        )
    return tuple(rows)
