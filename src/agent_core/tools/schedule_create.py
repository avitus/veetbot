"""Governed model-callable creation of calendar scheduled runs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from agent_core.application.services import ScheduleService
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.browser_task_grants import TaskGrantNotCovered
from agent_core.domain.errors import AuthorizationError, ConflictError, ScheduleValidationError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    AuthorizationTurn,
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunLimits
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
from agent_core.ports.persistence import UnitOfWorkFactory

SCHEDULE_CREATE_TOOL_NAME = "schedule.create"
SCHEDULE_WRITE_SCOPE = "schedule.write"
DEFAULT_RUN_TIMEOUT_SECONDS = 300
DEFAULT_MISFIRE_GRACE_SECONDS = 3600
DEFAULT_MAX_CONSECUTIVE_FAILURES = 2
DEFAULT_MAX_COST = Decimal("5")
# Final-synthesis headroom (ADR-0078): near its budget, a research run writes its
# answer from the evidence it already has instead of failing without one.
DEFAULT_SYNTHESIS_RESERVE_STEPS = 2
DEFAULT_SYNTHESIS_RESERVE_MODEL_CALLS = 2
DEFAULT_SYNTHESIS_RESERVE_COST = Decimal("1")

_FORBIDDEN_CADENCE_FIELD_SCHEMA: dict[str, Any] = {"not": {}}

# Keep the model contract closed without repeating the common civil-time and
# selector schemas in every variant. A property constrained by the always-false
# schema is forbidden for that cadence kind; execution still validates the same
# discriminated domain union before writing schedule state.
RECURRING_CADENCE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {},
        "local_time": {"type": "string", "format": "time"},
        "timezone": {"type": "string", "minLength": 1},
        "weekdays": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 7},
            "minItems": 1,
            "maxItems": 7,
            "uniqueItems": True,
        },
        "days_of_month": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 31},
            "maxItems": 31,
            "uniqueItems": True,
        },
        "last_day": {"type": "boolean"},
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
    },
    "required": ["kind", "local_time", "timezone"],
    "oneOf": [
        {
            "properties": {
                "kind": {"const": "DAILY"},
                "weekdays": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
                "days_of_month": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
                "last_day": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
                "dates": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
            },
        },
        {
            "properties": {
                "kind": {"const": "WEEKLY"},
                "days_of_month": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
                "last_day": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
                "dates": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
            },
            "required": ["weekdays"],
        },
        {
            "properties": {
                "kind": {"const": "MONTHLY"},
                "weekdays": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
                "dates": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
            },
            "required": ["days_of_month", "last_day"],
        },
        {
            "properties": {
                "kind": {"const": "YEARLY"},
                "weekdays": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
                "days_of_month": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
                "last_day": _FORBIDDEN_CADENCE_FIELD_SCHEMA,
            },
            "required": ["dates"],
        },
    ],
    "additionalProperties": False,
}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "use_website": {"type": "boolean"},
        "title": {"type": "string", "minLength": 1, "maxLength": 1024},
        "instruction": {"type": "string", "minLength": 1, "maxLength": 65_536},
        "at": {"type": "string", "format": "date-time"},
        "cadence": RECURRING_CADENCE_INPUT_SCHEMA,
    },
    "required": ["title", "instruction"],
    "oneOf": [
        {"required": ["at"], "not": {"required": ["cadence"]}},
        {"required": ["cadence"], "not": {"required": ["at"]}},
    ],
    "additionalProperties": False,
}
CADENCE_ADAPTER: TypeAdapter[Cadence] = TypeAdapter(Cadence)


class CadenceInputError(ValueError):
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
        version="1.2.0",
        description=(
            "Create one future one-time or recurring schedule. For recurrence, supply a "
            "complete IANA-zone cadence; ask about ambiguous dates or times. For tasks requiring "
            "a signed-in website, set use_website=true to pin this chat's selected website. "
            "Connect Website Access first if missing; never promise authenticated work without it."
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
        uow_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        self._service = service
        self._agent = agent
        self._limits = limits
        self._uow_factory = uow_factory

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
            "requested_scopes": ["browser.profile.read"] if arguments.get("use_website") else [],
            **({"use_website": True} if arguments.get("use_website") else {}),
        }
        try:
            cadence = parse_cadence(arguments)
        except CadenceInputError:
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

    async def approval_view_in_session(
        self,
        arguments: dict[str, Any],
        *,
        run: Run,
        principal: Principal,
        turn: AuthorizationTurn | None,
        not_covered: TaskGrantNotCovered | None,
    ) -> ApprovalPresentation:
        del turn, not_covered
        summary, proposal = await self.approval_view(arguments, tenant_id=principal.tenant_id)
        if arguments.get("use_website") and self._uow_factory is not None:
            async with self._uow_factory() as uow:
                session = await uow.sessions.get(run.session_id, principal)
                selected = session.metadata.get("browser_profile_id")
                proposal["browser_profile_id"] = selected
                if isinstance(selected, str):
                    profile = await uow.browser_profiles.get(UUID(selected), principal)
                    proposal["website_origins"] = list(profile.allowed_origins)
        return ApprovalPresentation(summary=summary, arguments=proposal)

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            cadence = parse_cadence(arguments)
        except CadenceInputError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                exc.reason_code,
                str(exc),
                retryable=True,
            )

        selected_profile = None
        if arguments.get("use_website"):
            if self._uow_factory is not None:
                async with self._uow_factory() as uow:
                    session = await uow.sessions.get(context.session_id, context.principal)
                    selected_profile = session.metadata.get("browser_profile_id")
            if not isinstance(selected_profile, str):
                return _failure(
                    ToolFailureKind.INVALID_ARGUMENTS,
                    "schedule.browser_profile_required",
                    "Connect Website Access in this conversation before creating this schedule.",
                    retryable=False,
                )
        definition = ScheduleDefinition(
            title=str(arguments["title"]),
            instruction=str(arguments["instruction"]),
            agent_id=self._agent.id,
            agent_version=self._agent.version,
            policy_profile=self._agent.policy_profile,
            requested_scopes=frozenset({"browser.profile.read"})
            if selected_profile
            else frozenset(),
            browser_profile_id=UUID(selected_profile) if selected_profile else None,
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
        max_steps = min(current.max_steps, self._limits.max_steps_per_run)
        max_model_calls = min(current.max_model_calls, self._limits.max_model_calls_per_run)
        max_cost = min(current.max_cost or DEFAULT_MAX_COST, self._limits.max_cost_per_run)
        return RunLimits(
            max_steps=max_steps,
            max_model_calls=max_model_calls,
            max_tool_calls=min(
                current.max_tool_calls,
                self._limits.max_tool_calls_per_run,
            ),
            max_input_tokens=current.max_input_tokens,
            max_output_tokens=current.max_output_tokens,
            max_cost=max_cost,
            synthesis_reserve_steps=_reserve_within(DEFAULT_SYNTHESIS_RESERVE_STEPS, max_steps),
            synthesis_reserve_model_calls=_reserve_within(
                DEFAULT_SYNTHESIS_RESERVE_MODEL_CALLS, max_model_calls
            ),
            synthesis_reserve_cost=_reserve_within(DEFAULT_SYNTHESIS_RESERVE_COST, max_cost),
        )


def _reserve_within[T: (int, Decimal)](reserve: T, limit: T) -> T:
    """Keep a reserve only when its limit leaves room to research before it."""

    return reserve if reserve < limit else type(reserve)(0)


def parse_cadence(arguments: dict[str, Any]) -> Cadence:
    has_at = "at" in arguments
    has_cadence = "cadence" in arguments
    if has_at == has_cadence:
        raise CadenceInputError(
            "schedule.cadence_invalid",
            "provide exactly one of at or cadence",
        )
    if has_at:
        try:
            at = datetime.fromisoformat(str(arguments["at"]).replace("Z", "+00:00"))
            return OnceCadence(at=at)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise CadenceInputError("schedule.instant_invalid", str(exc)) from exc
    try:
        cadence = CADENCE_ADAPTER.validate_python(arguments["cadence"])
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise CadenceInputError("schedule.cadence_invalid", str(exc)) from exc
    if isinstance(cadence, OnceCadence):
        raise CadenceInputError(
            "schedule.cadence_invalid",
            "cadence must be DAILY, WEEKLY, MONTHLY, or YEARLY",
        )
    return cadence


class LegacyScheduleCreateTool(ScheduleCreateTool):
    """Retain the closed input shown to already pinned runs."""

    spec = ScheduleCreateTool.spec.model_copy(
        update={
            "version": "1.1.1",
            "description": (
                "Create one future one-time or recurring schedule. For recurrence, supply a "
                "complete IANA-zone cadence; ask about ambiguous dates or times."
            ),
            "input_schema": {
                **INPUT_SCHEMA,
                "properties": {
                    key: value
                    for key, value in INPUT_SCHEMA["properties"].items()
                    if key != "use_website"
                },
            },
        }
    )
