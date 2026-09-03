"""Governed model-callable discovery and lifecycle for scheduled runs."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from agent_core.application.services import ScheduleService
from agent_core.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ScheduleValidationError,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.schedules import ScheduleRecord
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)

SCHEDULE_LIST_TOOL_NAME = "schedule.list"
SCHEDULE_PAUSE_TOOL_NAME = "schedule.pause"
SCHEDULE_RESUME_TOOL_NAME = "schedule.resume"
SCHEDULE_CANCEL_TOOL_NAME = "schedule.cancel"
SCHEDULE_LIFECYCLE_TOOL_NAMES = (
    SCHEDULE_LIST_TOOL_NAME,
    SCHEDULE_PAUSE_TOOL_NAME,
    SCHEDULE_RESUME_TOOL_NAME,
    SCHEDULE_CANCEL_TOOL_NAME,
)
SCHEDULE_READ_SCOPE = "schedule.read"
SCHEDULE_WRITE_SCOPE = "schedule.write"
SCHEDULE_CANCEL_SCOPE = "schedule.cancel"
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 50

CADENCE_OUTPUT_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "ONCE"},
                "at": {"type": "string", "format": "date-time"},
            },
            "required": ["kind", "at"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "DAILY"},
                "local_time": {"type": "string", "format": "time"},
                "timezone": {"type": "string"},
            },
            "required": ["kind", "local_time", "timezone"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "WEEKLY"},
                "local_time": {"type": "string", "format": "time"},
                "weekdays": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 7},
                    "minItems": 1,
                    "maxItems": 7,
                    "uniqueItems": True,
                },
                "timezone": {"type": "string"},
            },
            "required": ["kind", "local_time", "weekdays", "timezone"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "MONTHLY"},
                "local_time": {"type": "string", "format": "time"},
                "days_of_month": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 31},
                    "maxItems": 31,
                    "uniqueItems": True,
                },
                "last_day": {"type": "boolean"},
                "timezone": {"type": "string"},
            },
            "required": [
                "kind",
                "local_time",
                "days_of_month",
                "last_day",
                "timezone",
            ],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "YEARLY"},
                "local_time": {"type": "string", "format": "time"},
                "dates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "month": {"type": "integer", "minimum": 1, "maximum": 12},
                            "day": {"type": "integer", "minimum": 1, "maximum": 31},
                        },
                        "required": ["month", "day"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                    "maxItems": 366,
                    "uniqueItems": True,
                },
                "timezone": {"type": "string"},
            },
            "required": ["kind", "local_time", "dates", "timezone"],
            "additionalProperties": False,
        },
    ]
}

SUMMARY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schedule_id": {"type": "string", "format": "uuid"},
        "title": {"type": "string"},
        "state": {"enum": ["ACTIVE", "PAUSED", "COMPLETED", "CANCELLED"]},
        "pause_reason": {"type": ["string", "null"]},
        "current_revision": {"type": "integer", "minimum": 1},
        "next_fire_at": {"type": ["string", "null"], "format": "date-time"},
        "cadence": CADENCE_OUTPUT_SCHEMA,
    },
    "required": [
        "schedule_id",
        "title",
        "state",
        "pause_reason",
        "current_revision",
        "next_fire_at",
        "cadence",
    ],
    "additionalProperties": False,
}

LIST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIST_LIMIT,
            "default": DEFAULT_LIST_LIMIT,
        },
        "cursor": {"type": "string", "minLength": 1},
    },
    "required": [],
    "additionalProperties": False,
}

LIST_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "maxItems": MAX_LIST_LIMIT,
            "items": SUMMARY_OUTPUT_SCHEMA,
        },
        "next_cursor": {"type": ["string", "null"]},
    },
    "required": ["items", "next_cursor"],
    "additionalProperties": False,
}

MUTATION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schedule_id": {"type": "string", "format": "uuid"},
        "expected_revision": {"type": "integer", "minimum": 1},
    },
    "required": ["schedule_id", "expected_revision"],
    "additionalProperties": False,
}

MUTATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schedule_id": {"type": "string", "format": "uuid"},
        "current_revision": {"type": "integer", "minimum": 1},
        "state": {"enum": ["ACTIVE", "PAUSED", "COMPLETED", "CANCELLED"]},
        "pause_reason": {"type": ["string", "null"]},
        "next_fire_at": {"type": ["string", "null"], "format": "date-time"},
    },
    "required": [
        "schedule_id",
        "current_revision",
        "state",
        "pause_reason",
        "next_fire_at",
    ],
    "additionalProperties": False,
}


def _failure(
    kind: ToolFailureKind,
    reason_code: str,
    detail: str,
    *,
    message: str,
) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[TextPart(text=message)],
        failure=ToolFailure(
            kind=kind,
            reason_code=reason_code,
            detail=detail,
            retryable=False,
        ),
    )


def _summary(record: ScheduleRecord) -> dict[str, Any]:
    schedule = record.schedule
    return {
        "schedule_id": str(schedule.id),
        "title": record.revision.title,
        "state": schedule.state.value,
        "pause_reason": None if schedule.pause_reason is None else schedule.pause_reason.value,
        "current_revision": schedule.current_revision,
        "next_fire_at": (
            None if schedule.next_fire_at is None else schedule.next_fire_at.isoformat()
        ),
        "cadence": record.revision.cadence.model_dump(mode="json"),
    }


def _mutation_result(record: ScheduleRecord, action: str) -> ToolResult:
    schedule = record.schedule
    structured = {
        "schedule_id": str(schedule.id),
        "current_revision": schedule.current_revision,
        "state": schedule.state.value,
        "pause_reason": None if schedule.pause_reason is None else schedule.pause_reason.value,
        "next_fire_at": (
            None if schedule.next_fire_at is None else schedule.next_fire_at.isoformat()
        ),
    }
    return ToolResult(
        ok=True,
        content=[
            TextPart(
                text=(f"Schedule {schedule.id} is {schedule.state.value.lower()} after {action}.")
            )
        ],
        structured=structured,
    )


class ScheduleListTool:
    """List principal-owned schedule summaries without instruction content."""

    spec = ToolSpec(
        name=SCHEDULE_LIST_TOOL_NAME,
        version="1.0.0",
        description=(
            "List the user's schedules as bounded summaries. Use this before pause, resume, "
            "or cancel to resolve a description to one stable schedule ID and revision; ask "
            "the user if zero or multiple summaries match."
        ),
        input_schema=LIST_INPUT_SCHEMA,
        output_schema=LIST_OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes={SCHEDULE_READ_SCOPE},
        timeout_seconds=15,
        maximum_output_bytes=524_288,
        allow_parallel=True,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, service: ScheduleService) -> None:
        self._service = service

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        limit = arguments.get("limit", DEFAULT_LIST_LIMIT)
        cursor = arguments.get("cursor")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > MAX_LIST_LIMIT
        ):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "schedule.list_limit_invalid",
                "limit must be an integer from 1 through 50",
                message="The schedules could not be listed.",
            )
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "schedule.cursor_invalid",
                "cursor must be a non-empty opaque string",
                message="The schedules could not be listed.",
            )
        try:
            page = await self._service.list(context.principal, limit, cursor)
        except AuthorizationError as exc:
            return _failure(
                ToolFailureKind.PERMISSION,
                "policy.scope.missing",
                str(exc),
                message="The schedules could not be listed.",
            )
        except ValueError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "schedule.cursor_invalid",
                str(exc),
                message="The schedules could not be listed.",
            )
        structured = {
            "items": [_summary(record) for record in page.items],
            "next_cursor": page.next_cursor,
        }
        return ToolResult(
            ok=True,
            content=[TextPart(text=f"Found {len(page.items)} schedule summaries.")],
            structured=structured,
        )


LifecycleAction = Literal["pause", "resume", "cancel"]


def _past_tense(action: LifecycleAction) -> str:
    return {"pause": "paused", "resume": "resumed", "cancel": "cancelled"}[action]


def _mutation_spec(
    name: str,
    action: LifecycleAction,
    side_effect: SideEffectClass,
    scope: str,
) -> ToolSpec:
    verb = "Cancel" if action == "cancel" else action.capitalize()
    detail = (
        "Cancellation is terminal, preserves history, and does not cancel an already "
        "materialized run."
        if action == "cancel"
        else "Use schedule.list first and never select a schedule by title alone."
    )
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=(f"{verb} exactly one schedule by stable ID and expected revision. {detail}"),
        input_schema=MUTATION_INPUT_SCHEMA,
        output_schema=MUTATION_OUTPUT_SCHEMA,
        side_effect=side_effect,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes={scope},
        timeout_seconds=15,
        maximum_output_bytes=4096,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )


class _ScheduleMutationTool:
    action: LifecycleAction

    def __init__(self, service: ScheduleService) -> None:
        self._service = service

    async def approval_view(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
    ) -> tuple[str, dict[str, Any]]:
        del tenant_id
        schedule_id = str(arguments.get("schedule_id", "<invalid>"))
        expected_revision = arguments.get("expected_revision", "<invalid>")
        return (
            f"{self.action.capitalize()} schedule {schedule_id} at revision {expected_revision}",
            arguments,
        )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        past_tense = _past_tense(self.action)
        try:
            schedule_id = UUID(str(arguments["schedule_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "schedule.id_invalid",
                "schedule_id must be a UUID",
                message=f"The schedule was not {past_tense}.",
            )
        expected_revision = arguments.get("expected_revision")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "schedule.revision_invalid",
                "expected_revision must be a positive integer",
                message=f"The schedule was not {past_tense}.",
            )
        try:
            await context.mark_effect_sent()
            if self.action == "pause":
                record = await self._service.pause(
                    context.principal, schedule_id, expected_revision
                )
            elif self.action == "resume":
                record = await self._service.resume(
                    context.principal, schedule_id, expected_revision
                )
            else:
                record = await self._service.cancel(
                    context.principal, schedule_id, expected_revision
                )
        except NotFoundError as exc:
            return _failure(
                ToolFailureKind.NOT_FOUND,
                "schedule.not_found",
                str(exc),
                message=f"The schedule was not {past_tense}.",
            )
        except AuthorizationError as exc:
            return _failure(
                ToolFailureKind.PERMISSION,
                "policy.scope.missing",
                str(exc),
                message=f"The schedule was not {past_tense}.",
            )
        except ScheduleValidationError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                exc.reason,
                str(exc),
                message=f"The schedule was not {past_tense}.",
            )
        except ConflictError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                exc.reason or "schedule.lifecycle_conflict",
                str(exc),
                message=f"The schedule was not {past_tense}.",
            )
        return _mutation_result(record, self.action)


class SchedulePauseTool(_ScheduleMutationTool):
    action: LifecycleAction = "pause"
    spec = _mutation_spec(
        SCHEDULE_PAUSE_TOOL_NAME,
        action,
        SideEffectClass.EXTERNAL_WRITE,
        SCHEDULE_WRITE_SCOPE,
    )


class ScheduleResumeTool(_ScheduleMutationTool):
    action: LifecycleAction = "resume"
    spec = _mutation_spec(
        SCHEDULE_RESUME_TOOL_NAME,
        action,
        SideEffectClass.EXTERNAL_WRITE,
        SCHEDULE_WRITE_SCOPE,
    )


class ScheduleCancelTool(_ScheduleMutationTool):
    action: LifecycleAction = "cancel"
    spec = _mutation_spec(
        SCHEDULE_CANCEL_TOOL_NAME,
        action,
        SideEffectClass.EXTERNAL_DELETE,
        SCHEDULE_CANCEL_SCOPE,
    )
