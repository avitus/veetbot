"""Timezone conversion over the injected aware-UTC Clock port."""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.determinism import Clock

CURRENT_TIME_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timezone": {
            "type": "string",
            "maxLength": 64,
            "default": "UTC",
            "description": "IANA name, e.g. UTC or America/New_York",
        }
    },
    "required": [],
    "additionalProperties": False,
}

CURRENT_TIME_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "iso8601": {"type": "string"},
        "timezone": {"type": "string"},
        "utc_offset": {"type": "string"},
        "unix_seconds": {"type": "integer"},
        "weekday": {"type": "string"},
    },
    "required": ["iso8601", "timezone", "utc_offset", "unix_seconds", "weekday"],
    "additionalProperties": False,
}


def _offset_text(offset: timedelta) -> str:
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


class CurrentTimeTool:
    spec = ToolSpec(
        name="system.current_time",
        version="1.0.0",
        description="Return the injected current time converted to an IANA timezone.",
        input_schema=CURRENT_TIME_INPUT_SCHEMA,
        output_schema=CURRENT_TIME_OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(),
        timeout_seconds=2,
        maximum_output_bytes=4096,
        allow_parallel=True,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del context
        name = arguments.get("timezone", "UTC")
        if not isinstance(name, str) or (name != "UTC" and "/" not in name):
            return self._unknown_timezone(name)
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return self._unknown_timezone(name)
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock contract violation: now() must return an aware datetime")
        converted = now.astimezone(zone)
        offset = converted.utcoffset()
        if offset is None:
            raise ValueError("timezone conversion returned no UTC offset")
        iso8601 = converted.isoformat(timespec="microseconds")
        structured = {
            "iso8601": iso8601,
            "timezone": name,
            "utc_offset": _offset_text(offset),
            "unix_seconds": math.floor(converted.timestamp()),
            "weekday": converted.strftime("%A"),
        }
        return ToolResult(
            ok=True,
            content=[TextPart(text=f"{iso8601} ({name}, {structured['weekday']})")],
            structured=structured,
        )

    @staticmethod
    def _unknown_timezone(value: object) -> ToolResult:
        return ToolResult(
            ok=False,
            content=[],
            failure=ToolFailure(
                kind=ToolFailureKind.INVALID_ARGUMENTS,
                reason_code="tool.invalid_arguments.unknown_timezone",
                detail=f"unresolvable timezone argument of type {type(value).__name__}",
                retryable=False,
            ),
        )
