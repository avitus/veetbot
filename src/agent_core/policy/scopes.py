"""Closed platform scope vocabulary and MCP extension grammar."""

from __future__ import annotations

import re

from agent_core.domain.errors import ToolValidationError

PLATFORM_SCOPES = frozenset(
    {
        "session.read",
        "session.write",
        "run.read",
        "run.write",
        "run.cancel",
        "approval.read",
        "approval.resolve",
        "artifact.read",
        "artifact.write",
        "workspace.read",
        "workspace.write",
        "sandbox.execute",
        "skill.write",
        "demo.write",
        "knowledge.write",
        "browser.profile.read",
        "browser.profile.write",
        "browser.grant.read",
        "browser.grant.write",
    }
)
_SCOPE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


def validate_required_scopes(scopes: set[str], *, mcp_server_id: str | None = None) -> None:
    for scope in scopes:
        if _SCOPE.fullmatch(scope) is None:
            raise ToolValidationError(f"invalid required scope {scope!r}")
        if mcp_server_id is None:
            if scope not in PLATFORM_SCOPES:
                raise ToolValidationError(f"unknown platform scope {scope!r}")
        elif not scope.startswith(f"mcp.{mcp_server_id}."):
            raise ToolValidationError("MCP tools may require only their server scope namespace")


def missing_scopes(required: set[str], granted: set[str]) -> frozenset[str]:
    """Compare exact strings; scope names have no hierarchy or wildcard semantics."""

    return frozenset(required - granted)
