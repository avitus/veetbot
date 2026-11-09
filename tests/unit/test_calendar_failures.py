"""Calendar failures remain useful without disclosing provider error bodies."""

from __future__ import annotations

import pytest

from agent_core.domain.mcp import MCPCallResult, MCPRemoteTool
from agent_core.mcp.configuration import calendar_server_configs
from agent_core.mcp.mapping import map_discovered_tools
from agent_core.mcp.runtime import MCPRuntime


@pytest.mark.parametrize(
    "code,reason,retryable",
    [
        ("credential_rejected", "tool.server_unauthorized", False),
        ("not_selected", "tool.arguments_invalid", False),
        ("arguments_invalid", "tool.arguments_invalid", False),
        ("access_denied", "tool.permission_denied", False),
        ("not_found", "tool.not_found", False),
        ("provider_output_invalid", "tool.output_invalid", False),
        ("rate_limited", "tool.server_error", True),
        ("provider_unavailable", "tool.server_error", True),
    ],
)
def test_calendar_errors_have_safe_actionable_failures(
    code: str, reason: str, retryable: bool
) -> None:
    (row,) = calendar_server_configs("local")
    remote = MCPRemoteTool(
        name="get_event",
        description="Read an event.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    spec = map_discovered_tools(row, (remote,)).accepted[0].spec
    result = MCPRuntime._result(
        MCPCallResult(
            is_error=True,
            content=(f"calendar.{code}",),
            structured={"effect_status": "not_applied"},
        ),
        spec,
    )
    assert result.failure is not None
    assert result.failure.reason_code == reason
    assert result.failure.retryable is retryable
    assert result.failure.external_text is None
    assert "Calendar" in result.failure.detail
