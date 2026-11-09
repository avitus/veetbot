"""Closed, content-free failures for the operator-owned Calendar server."""

from __future__ import annotations

from agent_core.domain.mcp import MCPCallResult
from agent_core.domain.tools import ToolFailure, ToolFailureKind, ToolSpec

_ERRORS = {
    "calendar.credential_rejected": (
        ToolFailureKind.TRANSPORT,
        "tool.server_unauthorized",
        "Google Calendar authorization expired or was revoked. Reconnect the Calendar account.",
    ),
    "calendar.not_selected": (
        ToolFailureKind.INVALID_ARGUMENTS,
        "tool.arguments_invalid",
        "This Calendar is not selected. Use list_calendars to find permitted calendar IDs.",
    ),
    "calendar.arguments_invalid": (
        ToolFailureKind.INVALID_ARGUMENTS,
        "tool.arguments_invalid",
        "The Calendar request is invalid. Check the tool schema, IDs and date window.",
    ),
    "calendar.access_denied": (
        ToolFailureKind.PERMISSION,
        "tool.permission_denied",
        "Google Calendar denied access. Check the account and calendar permissions.",
    ),
    "calendar.not_found": (
        ToolFailureKind.NOT_FOUND,
        "tool.not_found",
        "The requested Google Calendar event or calendar was not found.",
    ),
    "calendar.provider_output_invalid": (
        ToolFailureKind.OUTPUT_INVALID,
        "tool.output_invalid",
        "Google Calendar returned invalid or oversized data. This is not an empty agenda.",
    ),
    "calendar.rate_limited": (
        ToolFailureKind.UPSTREAM_ERROR,
        "tool.server_error",
        "Google Calendar is rate limited. Try again later.",
    ),
    "calendar.provider_unavailable": (
        ToolFailureKind.UPSTREAM_ERROR,
        "tool.server_error",
        "Google Calendar is temporarily unavailable. Try again later.",
    ),
    "calendar.provider_rejected": (
        ToolFailureKind.UPSTREAM_ERROR,
        "tool.server_error",
        "Google Calendar rejected the request.",
    ),
}


def calendar_failure(result: MCPCallResult, spec: ToolSpec) -> ToolFailure | None:
    """Map only recognized first-party Calendar errors to safe runtime failures."""
    if (
        not result.is_error
        or not spec.name.startswith("mcp.calendar_read.")
        or len(result.content) != 1
    ):
        return None
    code = result.content[0]
    if code not in _ERRORS:
        return None
    kind, reason, detail = _ERRORS[code]
    return ToolFailure(
        kind=kind,
        reason_code=reason,
        detail=detail,
        retryable=code in {"calendar.rate_limited", "calendar.provider_unavailable"},
    )
