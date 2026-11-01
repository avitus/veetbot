"""Read why the owner's own runs failed and where their time went (ADR-0281)."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any
from uuid import UUID

from agent_core.domain.code import RUN_INSPECT_TOOL_NAME
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.runs import FailureReason, Run
from agent_core.domain.schedules import OccurrenceDisposition
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.persistence import UnitOfWorkFactory

_DEFAULT_SCAN = 50
_MAXIMUM_SCAN = 200
_MAXIMUM_FAILURES = 8
_PAGE_EVENTS = 100
# Tool results reach the model whole only up to the 4 KiB inline budget.
_PAGE_BYTES = 3400
_LOG_CHARACTERS = 300
_FAILURE_CODE = re.compile(r"^job\.[a-z_]+$")
# Platform-authored wording; a run's other failure messages can quote what it read.
_PLATFORM_ERROR_CLASSES = frozenset({"RunDeadlineExceeded"})

_HINTS = {
    "job.response_limit": (
        "One HTTP response passed the job's byte ceiling (48 KiB at most). A caught refusal "
        "still fails the run. Ask for fewer fields, such as a GraphQL query, or smaller pages."
    ),
    "job.request_limit": (
        "The job used all its approved HTTP requests. Make fewer calls or batch them."
    ),
    "job.host_refused": "The URL's host is not in the job's approved grant.",
    "job.feed_refused": "The job published a feed its grant does not name.",
    "job.grant_missing": "The schedule's pinned grant no longer exists; approve a new one.",
    "job.revision_unpublished": "The pinned job revision has no commit; publish it first.",
    "job.disabled": "Jobs are switched off on this server.",
    "job.model_budget_exceeded": "The job spent its approved model budget.",
    FailureReason.DEADLINE_EXCEEDED.value: (
        "The run passed its timeout, which also covers queueing, sandbox start and cleanup. "
        "Compare the timeline's gaps with the schedule's run_timeout_seconds."
    ),
}


def _failure(kind: ToolFailureKind, reason: str, detail: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(kind=kind, reason_code=reason, detail=detail, retryable=False),
    )


def _not_found() -> ToolResult:
    return _failure(
        ToolFailureKind.NOT_FOUND,
        "run.inspect_not_found",
        "No such run or schedule. A deleted chat erases its runs' links.",
    )


def _code(run: Run) -> str | None:
    """The failure's machine code, never free text a program or provider wrote."""
    message = run.failure.message if run.failure else ""
    return message if _FAILURE_CODE.fullmatch(message) else None


def _hint(run: Run) -> str | None:
    if run.failure is None:
        return None
    return _HINTS.get(_code(run) or run.failure.reason.value)


def _seconds(run: Run) -> float:
    return round((run.updated_at - run.created_at).total_seconds(), 1)


def _timeline_line(event: EventEnvelope, base: Run) -> tuple[str, bool]:
    """One event as a line, plus whether it carries text a program wrote."""
    offset = (event.created_at - base.created_at).total_seconds()
    payload = event.payload
    kind = event.event_type
    parts = [f"+{offset:.1f}s", kind]
    untrusted = False
    if kind == "job.log":
        parts.append(str(payload.get("message", ""))[:_LOG_CHARACTERS])
        untrusted = True
    elif kind.startswith("job.broker."):
        parts.extend(
            str(payload[key])
            for key in ("operation", "outcome", "reason_code")
            if payload.get(key) is not None
        )
        if isinstance(payload.get("duration_ms"), int):
            parts.append(f"{payload['duration_ms']}ms")
    elif kind.startswith("tool.call."):
        # Only the name: a tool's arguments and result can be long or private.
        parts.extend(
            str(payload[key]) for key in ("name", "reason_code") if payload.get(key) is not None
        )
    elif kind == "run.claimed":
        parts.append(f"attempt {payload.get('attempt')} on {payload.get('worker_id')}")
    return " ".join(parts), untrusted


class RunInspectTool:
    spec = ToolSpec(
        name=RUN_INSPECT_TOOL_NAME,
        version="1.0.0",
        description=(
            "Read why scheduled runs failed and where their time went, before calling a cause "
            "unknown. "
            "schedule_history: pause state, limits and recent failures with repair hints, "
            "for schedule_id from schedule.list. run: one run's status, failure and timed "
            "event timeline, for run_id. A widget's device failures are in cockpit.read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["schedule_history", "run"]},
                "schedule_id": {"type": "string", "format": "uuid"},
                "run_id": {"type": "string", "format": "uuid"},
                "scan": {"type": "integer", "minimum": 1, "maximum": _MAXIMUM_SCAN},
                "after_sequence": {"type": "integer", "minimum": 0},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes={"run.read"},
        timeout_seconds=30,
        maximum_output_bytes=16 * 1024,
        allow_parallel=True,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        operation = arguments["operation"]
        identifier = arguments.get("schedule_id" if operation == "schedule_history" else "run_id")
        if not identifier:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "run.inspect_id_required",
                f"{operation} needs its identifier.",
            )
        if operation == "schedule_history" and "schedule.read" not in context.principal.scopes:
            return _failure(
                ToolFailureKind.PERMISSION,
                "run.inspect_schedule_scope",
                "Reading a schedule's history needs schedule.read.",
            )
        try:
            if operation == "schedule_history":
                return await self._schedule_history(
                    UUID(identifier), arguments.get("scan", _DEFAULT_SCAN), context
                )
            return await self._run(UUID(identifier), arguments.get("after_sequence", 0), context)
        except NotFoundError:
            return _not_found()

    async def _schedule_history(
        self, schedule_id: UUID, scan: int, context: ToolExecutionContext
    ) -> ToolResult:
        principal = context.principal
        counts: Counter[str] = Counter()
        failures: list[dict[str, Any]] = []
        async with self._uow_factory() as uow:
            schedule = await uow.schedules.get(schedule_id, principal)
            revision = await uow.schedules.get_revision(
                schedule_id, schedule.current_revision, principal
            )
            occurrences = await uow.schedule_occurrences.list(schedule_id, principal, limit=scan)
            for occurrence in occurrences:
                entry: dict[str, Any] = {
                    "fired_at": occurrence.nominal_fire_at.isoformat(),
                    "trigger": occurrence.trigger.value,
                }
                if occurrence.run_id is None:
                    if occurrence.disposition is OccurrenceDisposition.MATERIALIZED:
                        counts["LINK_ERASED"] += 1
                        continue
                    counts[occurrence.disposition.value] += 1
                    entry.update(
                        disposition=occurrence.disposition.value, code=occurrence.reason_code
                    )
                else:
                    try:
                        run = await uow.runs.get(occurrence.run_id, principal)
                    except NotFoundError:
                        counts["LINK_ERASED"] += 1
                        continue
                    counts[run.status.value] += 1
                    if run.failure is None:
                        continue
                    entry.update(
                        run_id=str(run.id),
                        reason=run.failure.reason.value,
                        error_class=run.failure.error_class,
                        code=_code(run),
                        hint=_hint(run),
                        seconds=_seconds(run),
                    )
                if len(failures) < _MAXIMUM_FAILURES:
                    failures.append(entry)
        body = {
            "title": revision.title,
            "state": schedule.state.value,
            "pause_reason": schedule.pause_reason.value if schedule.pause_reason else None,
            "consecutive_failures": schedule.consecutive_failures,
            "max_consecutive_failures": revision.max_consecutive_failures,
            "run_timeout_seconds": revision.run_timeout_seconds,
            "scanned": len(occurrences),
            "counts": dict(counts),
            "failures": failures,
        }
        return ToolResult(
            ok=True,
            content=[TextPart(text=json.dumps(body, separators=(",", ":")))],
            structured=body,
            output_trust=TrustLevel.INTERNAL_TOOL,
        )

    async def _run(
        self, run_id: UUID, after_sequence: int, context: ToolExecutionContext
    ) -> ToolResult:
        principal = context.principal
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, principal)
            events = await uow.events.list_after(
                run.session_id, after_sequence, principal, run_id=run_id, limit=_PAGE_EVENTS + 1
            )
        failure: dict[str, Any] | None = None
        untrusted = False
        if run.failure is not None:
            failure = {
                "reason": run.failure.reason.value,
                "error_class": run.failure.error_class,
                "message": run.failure.message[:_LOG_CHARACTERS],
                "hint": _hint(run),
            }
            untrusted = (
                _code(run) is None and run.failure.error_class not in _PLATFORM_ERROR_CLASSES
            )
        header = {
            "status": run.status.value,
            "seconds": _seconds(run),
            "steps": run.step_count,
            "failure": failure,
        }
        lines: list[str] = []
        size = len(json.dumps(header).encode())
        next_after: int | None = None
        for index, event in enumerate(events):
            line, wrote = _timeline_line(event, run)
            size += len(line.encode()) + 1
            if size > _PAGE_BYTES or index == _PAGE_EVENTS:
                next_after = events[index - 1].sequence if index else after_sequence
                break
            lines.append(line)
            untrusted = untrusted or wrote
        body = {**header, "next_after_sequence": next_after}
        text = json.dumps(body, separators=(",", ":")) + "\n" + "\n".join(lines)
        return ToolResult(
            ok=True,
            content=[TextPart(text=text)],
            structured=body,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED if untrusted else TrustLevel.INTERNAL_TOOL,
        )
