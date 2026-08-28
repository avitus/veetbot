"""Governed model-callable creation of calendar scheduled runs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter, ValidationError

from agent_core.application.services import ScheduleService
from agent_core.domain.agents import AgentSpec
from agent_core.domain.errors import AuthorizationError, ConflictError, ScheduleValidationError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.runs import RunLimits
from agent_core.domain.schedules import (
    Cadence,
    OnceCadence,
    ScheduleDefinition,
    ScheduleDefinitionLimits,
)
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)

SCHEDULE_CREATE_TOOL_NAME = "schedule.create"
SCHEDULE_WRITE_SCOPE = "schedule.write"
DEFAULT_RUN_TIMEOUT_SECONDS = 300
DEFAULT_MISFIRE_GRACE_SECONDS = 3600
DEFAULT_MAX_CONSECUTIVE_FAILURES = 1
DEFAULT_MAX_COST = Decimal("1")

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 1024},
        "instruction": {"type": "string", "minLength": 1, "maxLength": 65_536},
        "at": {"type": "string", "format": "date-time"},
        "cadence": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "DAILY"},
                        "local_time": {"type": "string", "format": "time"},
                        "timezone": {"type": "string", "minLength": 1},
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
                        "timezone": {"type": "string", "minLength": 1},
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
                        "timezone": {"type": "string", "minLength": 1},
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
                        "timezone": {"type": "string", "minLength": 1},
                    },
                    "required": ["kind", "local_time", "dates", "timezone"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "required": ["title", "instruction"],
    "oneOf": [
        {"required": ["at"], "not": {"required": ["cadence"]}},
        {"required": ["cadence"], "not": {"required": ["at"]}},
    ],
    "additionalProperties": False,
}
CADENCE_ADAPTER: TypeAdapter[Cadence] = TypeAdapter(Cadence)


class _CadenceInputError(ValueError):
    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schedule_id": {"type": "string", "format": "uuid"},
        # A fresh creation is always ACTIVE; an idempotent replay reports the
        # schedule's current state, which may have moved on since creation.
        "state": {
            "type": "string",
            "enum": ["ACTIVE", "PAUSED", "COMPLETED", "CANCELLED"],
        },
        "next_fire_at": {"type": ["string", "null"], "format": "date-time"},
        "replayed": {"type": "boolean"},
    },
    "required": ["schedule_id", "state", "next_fire_at", "replayed"],
    "additionalProperties": False,
}


def _failure(
    kind: ToolFailureKind,
    reason_code: str,
    detail: str,
    *,
    retryable: bool,
) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[TextPart(text="The schedule was not created.")],
        failure=ToolFailure(
            kind=kind,
            reason_code=reason_code,
            detail=detail,
            retryable=retryable,
        ),
    )


class ScheduleCreateTool:
    """Create an approval-gated schedule with no delegated tool scopes."""

    spec = ToolSpec(
        name=SCHEDULE_CREATE_TOOL_NAME,
        version="1.1.0",
        description=(
            "Create one future one-time, daily, weekly, monthly, or yearly scheduled run. "
            "Use an exact aware instant or a complete IANA-zone civil cadence; ask the user "
            "when the date, calendar selector, local time, or timezone is ambiguous."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        required_scopes={SCHEDULE_WRITE_SCOPE},
        timeout_seconds=15,
        maximum_output_bytes=4096,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(
        self,
        service: ScheduleService,
        agent: AgentSpec,
        limits: ScheduleDefinitionLimits,
    ) -> None:
        self._service = service
        self._agent = agent
        self._limits = limits

    async def approval_view(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
    ) -> tuple[str, dict[str, Any]]:
        del tenant_id
        title = str(arguments["title"])
        proposal: dict[str, Any] = {
            "title": title,
            "instruction": str(arguments["instruction"]),
            "requested_scopes": [],
        }
        try:
            cadence = _parse_cadence(arguments)
        except _CadenceInputError:
            proposal.update({key: arguments[key] for key in ("at", "cadence") if key in arguments})
            return f"Create schedule {title!r}", proposal
        if isinstance(cadence, OnceCadence):
            proposal["at"] = cadence.at.isoformat()
            summary = f"Create one-time schedule {title!r} for {cadence.at.isoformat()}"
        else:
            proposal["cadence"] = cadence.model_dump(mode="json")
            summary = f"Create {cadence.kind.value.lower()} schedule {title!r}"
        return (
            summary,
            proposal,
        )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            cadence = _parse_cadence(arguments)
        except _CadenceInputError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                exc.reason_code,
                str(exc),
                retryable=True,
            )

        definition = ScheduleDefinition(
            title=str(arguments["title"]),
            instruction=str(arguments["instruction"]),
            agent_id=self._agent.id,
            agent_version=self._agent.version,
            policy_profile=self._agent.policy_profile,
            requested_scopes=frozenset(),
            limits=self._run_limits(),
            run_timeout_seconds=min(
                DEFAULT_RUN_TIMEOUT_SECONDS,
                self._limits.max_run_timeout_seconds,
            ),
            cadence=cadence,
            misfire_grace_seconds=min(
                DEFAULT_MISFIRE_GRACE_SECONDS,
                self._limits.max_misfire_grace_seconds,
            ),
            max_consecutive_failures=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        )
        try:
            await context.mark_effect_sent()
            record = await self._service.create(
                context.principal,
                definition,
                context.idempotency_key,
            )
        except ScheduleValidationError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                exc.reason,
                str(exc),
                retryable=True,
            )
        except AuthorizationError as exc:
            return _failure(
                ToolFailureKind.PERMISSION,
                "policy.scope.missing",
                str(exc),
                retryable=False,
            )
        except ConflictError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                exc.reason or "schedule.create_conflict",
                str(exc),
                retryable=False,
            )

        next_fire_at = record.schedule.next_fire_at
        structured = {
            "schedule_id": str(record.schedule.id),
            "state": record.schedule.state.value,
            "next_fire_at": None if next_fire_at is None else next_fire_at.isoformat(),
            "replayed": record.replayed,
        }
        if next_fire_at is not None:
            narration = f"Created schedule {record.schedule.id} for {next_fire_at.isoformat()}."
        else:
            narration = (
                f"Schedule {record.schedule.id} already exists and is "
                f"{record.schedule.state.value.lower()}; it will not fire again."
            )
        return ToolResult(
            ok=True,
            content=[TextPart(text=narration)],
            structured=structured,
        )

    def _run_limits(self) -> RunLimits:
        current = self._agent.limits
        return RunLimits(
            max_steps=min(current.max_steps, self._limits.max_steps_per_run),
            max_model_calls=min(
                current.max_model_calls,
                self._limits.max_model_calls_per_run,
            ),
            max_tool_calls=min(
                current.max_tool_calls,
                self._limits.max_tool_calls_per_run,
            ),
            max_input_tokens=current.max_input_tokens,
            max_output_tokens=current.max_output_tokens,
            max_cost=min(
                current.max_cost or DEFAULT_MAX_COST,
                self._limits.max_cost_per_run,
            ),
        )


def _parse_cadence(arguments: dict[str, Any]) -> Cadence:
    has_at = "at" in arguments
    has_cadence = "cadence" in arguments
    if has_at == has_cadence:
        raise _CadenceInputError(
            "schedule.cadence_invalid",
            "provide exactly one of at or cadence",
        )
    if has_at:
        try:
            at = datetime.fromisoformat(str(arguments["at"]).replace("Z", "+00:00"))
            return OnceCadence(at=at)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise _CadenceInputError("schedule.instant_invalid", str(exc)) from exc
    try:
        cadence = CADENCE_ADAPTER.validate_python(arguments["cadence"])
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise _CadenceInputError("schedule.cadence_invalid", str(exc)) from exc
    if isinstance(cadence, OnceCadence):
        raise _CadenceInputError(
            "schedule.cadence_invalid",
            "cadence must be DAILY, WEEKLY, MONTHLY, or YEARLY",
        )
    return cadence
