"""Governed model-callable creation of calendar scheduled runs."""

from __future__ import annotations

from datetime import datetime
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
    CallPlan,
    OnceCadence,
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
from agent_core.scheduling.defaults import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    DEFAULT_MAX_COST,
    DEFAULT_MISFIRE_GRACE_SECONDS,
    DEFAULT_RUN_TIMEOUT_SECONDS,
    DEFAULT_SYNTHESIS_RESERVE_COST,
    DEFAULT_SYNTHESIS_RESERVE_MODEL_CALLS,
    DEFAULT_SYNTHESIS_RESERVE_STEPS,
    derived_definition,
    schedule_run_limits,
)

__all__ = [
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "DEFAULT_MAX_COST",
    "DEFAULT_MISFIRE_GRACE_SECONDS",
    "DEFAULT_RUN_TIMEOUT_SECONDS",
    "DEFAULT_SYNTHESIS_RESERVE_COST",
    "DEFAULT_SYNTHESIS_RESERVE_MODEL_CALLS",
    "DEFAULT_SYNTHESIS_RESERVE_STEPS",
]

SCHEDULE_CREATE_TOOL_NAME = "schedule.create"
SCHEDULE_WRITE_SCOPE = "schedule.write"
# ADR-0213: a call schedule delegates exactly the calling scopes, so its
# occurrence can place the call and read back how it went.
CALL_PLACE_SCOPE = "mcp.bland_call.use"
CALL_SCOPES = frozenset({CALL_PLACE_SCOPE, "mcp.bland_read.use"})
# Those scopes are all the occurrence holds, so it can never schedule a later
# attempt; Chat must not confirm a retry or follow-up it cannot arrange.
CALL_SCHEDULE_LIMITS = (
    "When it fires, its run can place calls but cannot create or change schedules or "
    "wait for a later time, so no retry or follow-up that depends on how a call went is "
    "arranged. If the owner asked for one, say it will not happen."
)
CALL_PLAN_NARRATION = (
    "When it fires, its run places the first call. The platform places each retry and "
    "the follow-up this call plan sets from the calls' results, then reports the outcome "
    "in this schedule; it arranges nothing else."
)

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

SINGLE_CALL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "use_website": {"type": "boolean"},
        "place_call": {"type": "boolean"},
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
# ADR-0221: retries and a follow-up the platform places from each call's result.
CALL_PLAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "retry_every_minutes": {"type": "integer", "minimum": 1, "maximum": 30},
        "retry_until": {"type": "string", "format": "date-time"},
        "follow_up_after_answer_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
    },
    "additionalProperties": False,
}
INPUT_SCHEMA: dict[str, Any] = {
    **SINGLE_CALL_INPUT_SCHEMA,
    "properties": {**SINGLE_CALL_INPUT_SCHEMA["properties"], "call_plan": CALL_PLAN_INPUT_SCHEMA},
}
CADENCE_ADAPTER: TypeAdapter[Cadence] = TypeAdapter(Cadence)


class CadenceInputError(ValueError):
    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


class CallPlanInputError(ValueError):
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


# Version 1.3.0 (ADR-0213), kept byte-identical for runs already pinned to it.
_SINGLE_CALL_SPEC = ToolSpec(
    name=SCHEDULE_CREATE_TOOL_NAME,
    version="1.3.0",
    description=(
        "Create one future one-time or recurring schedule. For recurrence, supply a "
        "complete IANA-zone cadence; ask about ambiguous dates or times. For tasks requiring "
        "a signed-in website, set use_website=true to pin this chat's selected website. "
        "Connect Website Access first if missing; never promise authenticated work without it. "
        "When the scheduled task is to place a phone call, set place_call=true and put the "
        "recipient's E.164 number, the brief and any voicemail text in the instruction; "
        "without place_call the scheduled run cannot call."
    ),
    input_schema=SINGLE_CALL_INPUT_SCHEMA,
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


class ScheduleCreateTool:
    """Create an approval-gated schedule that delegates only what its flags select."""

    spec = _SINGLE_CALL_SPEC.model_copy(
        update={
            "version": "1.4.0",
            "description": (
                "Create one future one-time or recurring schedule. For recurrence, supply a "
                "complete IANA-zone cadence; ask about ambiguous dates or times. For tasks "
                "requiring a signed-in website, set use_website=true to pin this chat's selected "
                "website. Connect Website Access first if missing; never promise authenticated "
                "work without it. When the scheduled task is to place a phone call, set "
                "place_call=true and put the recipient's E.164 number, the brief and any "
                "voicemail text in the instruction; without place_call the scheduled run cannot "
                "call. For a one-time call that should be retried until answered or followed up "
                "after an answer, also set call_plan: retry_every_minutes with retry_until (at "
                "most ten calls within an hour of at) and/or follow_up_after_answer_minutes. The "
                "platform places those calls from each call's result and reports the outcome; "
                "without call_plan no retry or follow-up happens."
            ),
            "input_schema": INPUT_SCHEMA,
        }
    )

    def __init__(
        self,
        service: ScheduleService,
        agent: AgentSpec,
        limits: ScheduleDefinitionLimits,
        uow_factory: UnitOfWorkFactory | None = None,
        *,
        calling_enabled: bool = False,
    ) -> None:
        self._service = service
        self._agent = agent
        self._limits = limits
        self._uow_factory = uow_factory
        self._calling_enabled = calling_enabled

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
            "requested_scopes": sorted(_requested_scopes(arguments)),
            **({"use_website": True} if arguments.get("use_website") else {}),
            **({"place_call": True} if arguments.get("place_call") else {}),
            **(
                {"call_plan": arguments["call_plan"]}
                if arguments.get("call_plan") is not None
                else {}
            ),
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

        if arguments.get("place_call") and not self._calling_enabled:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "schedule.calling_unavailable",
                "Calling is not set up, so a scheduled call could not be placed.",
                retryable=False,
            )
        try:
            call_plan = _call_plan(arguments, cadence)
        except CallPlanInputError as exc:
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
        definition = derived_definition(
            self._agent,
            self._limits,
            title=str(arguments["title"]),
            instruction=str(arguments["instruction"]),
            cadence=cadence,
            requested_scopes=_requested_scopes(arguments),
            browser_profile_id=UUID(selected_profile) if selected_profile else None,
            call_plan=call_plan,
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
        if call_plan is not None:
            narration = f"{narration} {CALL_PLAN_NARRATION}"
        elif arguments.get("place_call"):
            narration = f"{narration} {CALL_SCHEDULE_LIMITS}"
        return ToolResult(
            ok=True,
            content=[TextPart(text=narration)],
            structured=structured,
        )

    def _run_limits(self) -> RunLimits:
        return schedule_run_limits(self._agent, self._limits)


def _requested_scopes(arguments: dict[str, Any]) -> frozenset[str]:
    """The model selects capabilities by flag; it never names a scope.

    A planned call's run reads no call record: the platform reads each result,
    so the run keeps the owner-request authority ADR-0213 gives (ADR-0221).
    """

    calling = (
        ({CALL_PLACE_SCOPE} if arguments.get("call_plan") is not None else CALL_SCOPES)
        if arguments.get("place_call")
        else frozenset()
    )
    return frozenset(
        {*(("browser.profile.read",) if arguments.get("use_website") else ()), *calling}
    )


def _call_plan(arguments: dict[str, Any], cadence: Cadence) -> CallPlan | None:
    """ADR-0221: a call plan follows one call schedule's single first call."""

    raw = arguments.get("call_plan")
    if raw is None:
        return None
    if not arguments.get("place_call"):
        raise CallPlanInputError(
            "schedule.call_plan_needs_call", "a call plan needs place_call=true"
        )
    if not isinstance(cadence, OnceCadence):
        raise CallPlanInputError(
            "schedule.call_plan_needs_one_time", "a call plan needs a one-time schedule"
        )
    try:
        plan = CallPlan.model_validate(raw)
        plan.validate_for(cadence.at)
    except (ValidationError, ValueError) as exc:
        raise CallPlanInputError("schedule.call_plan_invalid", str(exc)) from exc
    return plan


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


class SingleCallScheduleCreateTool(ScheduleCreateTool):
    """Retain version 1.3.0, whose call schedules had no call plan, for pinned runs."""

    spec = _SINGLE_CALL_SPEC


class PreviousScheduleCreateTool(ScheduleCreateTool):
    """Retain version 1.2.0, which had no call schedules, for already pinned runs."""

    spec = _SINGLE_CALL_SPEC.model_copy(
        update={
            "version": "1.2.0",
            "description": (
                "Create one future one-time or recurring schedule. For recurrence, supply a "
                "complete IANA-zone cadence; ask about ambiguous dates or times. For tasks "
                "requiring a signed-in website, set use_website=true to pin this chat's selected "
                "website. Connect Website Access first if missing; never promise authenticated "
                "work without it."
            ),
            "input_schema": {
                **SINGLE_CALL_INPUT_SCHEMA,
                "properties": {
                    key: value
                    for key, value in SINGLE_CALL_INPUT_SCHEMA["properties"].items()
                    if key != "place_call"
                },
            },
        }
    )


class LegacyScheduleCreateTool(ScheduleCreateTool):
    """Retain the closed input shown to already pinned runs."""

    spec = _SINGLE_CALL_SPEC.model_copy(
        update={
            "version": "1.1.1",
            "description": (
                "Create one future one-time or recurring schedule. For recurrence, supply a "
                "complete IANA-zone cadence; ask about ambiguous dates or times."
            ),
            "input_schema": {
                **SINGLE_CALL_INPUT_SCHEMA,
                "properties": {
                    key: value
                    for key, value in SINGLE_CALL_INPUT_SCHEMA["properties"].items()
                    if key not in {"use_website", "place_call"}
                },
            },
        }
    )
