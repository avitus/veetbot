"""ADR-0221: the next step of a call plan, from each call's closed result."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agent_core.domain.schedules import CallPlan

# A call lasts at most five minutes and reconciliation polls every few seconds,
# so a result missing this long means something failed; the plan stops.
RESULT_WAIT = timedelta(minutes=20)
# The decided step is a process event: the scheduler cannot read call results,
# so the maintenance sweep records the next step and the scheduler reads it.
CALL_PLAN_STEP_EVENT = "schedule.call_plan.step"
CALL_PLAN_SESSION_METADATA_KEY = "call_plan_step"


def call_plan_step_key(schedule_id: UUID, attempt: int) -> str:
    return f"{CALL_PLAN_STEP_EVENT}:{schedule_id}:{attempt}"


class CallPlanStepKind(StrEnum):
    FIRST = "first"
    RETRY = "retry"
    FOLLOW_UP = "follow_up"
    REPORT = "report"


class CallResult(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    FAILED = "failed"
    NO_CALL = "no_call"
    UNKNOWN = "unknown"


class CallPlanReportReason(StrEnum):
    ANSWERED = "answered"
    NOT_REACHED = "not_reached"
    FOLLOW_UP_ANSWERED = "follow_up_answered"
    FOLLOW_UP_NOT_REACHED = "follow_up_not_reached"
    NO_CALL = "no_call"
    UNKNOWN = "unknown"


class CallPlanAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CallPlanStepKind
    nominal_fire_at: datetime
    materialized_at: datetime
    result: CallResult
    ended_at: datetime | None = None


class CallPlanHistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nominal_fire_at: datetime
    kind: CallPlanStepKind
    result: CallResult


class CallPlanStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt: int
    kind: CallPlanStepKind
    fire_at: datetime
    reason: CallPlanReportReason | None = None
    history: tuple[CallPlanHistoryEntry, ...] = ()
    answered_at: datetime | None = None


# A call that rang out, met a busy line or voicemail, or did not connect.
NOT_REACHED = frozenset(
    {CallResult.NO_ANSWER, CallResult.BUSY, CallResult.VOICEMAIL, CallResult.FAILED}
)
_CALLS = frozenset({CallPlanStepKind.FIRST, CallPlanStepKind.RETRY})
_WORDS = {
    CallResult.ANSWERED: "answered",
    CallResult.NO_ANSWER: "no answer",
    CallResult.BUSY: "busy",
    CallResult.VOICEMAIL: "voicemail",
    CallResult.FAILED: "did not connect",
    CallResult.NO_CALL: "no call placed",
    CallResult.UNKNOWN: "outcome unknown",
    CallResult.PENDING: "outcome unknown",
}
_REPORTS = {
    CallPlanReportReason.ANSWERED: "The call was answered.",
    CallPlanReportReason.NOT_REACHED: "No call was answered.",
    CallPlanReportReason.FOLLOW_UP_ANSWERED: "The call and its follow-up were both answered.",
    CallPlanReportReason.FOLLOW_UP_NOT_REACHED: (
        "The call was answered, but the follow-up was not."
    ),
    CallPlanReportReason.NO_CALL: "A planned call was not placed, so the plan stopped.",
    CallPlanReportReason.UNKNOWN: (
        "A call's outcome could not be confirmed, so no further call was placed."
    ),
}


def next_call_plan_step(
    plan: CallPlan,
    first_at: datetime,
    attempts: list[CallPlanAttempt],
    now: datetime,
) -> CallPlanStep | None:
    """Decide what follows the latest call, or None while its result is awaited.

    Only the closed result of each call is read, never what was said. A call
    whose result is missing or unknown ends the plan rather than risk a second
    call to someone already on the line.
    """
    last = attempts[-1]
    if last.result is CallResult.PENDING and now - last.materialized_at < RESULT_WAIT:
        return None
    history = tuple(
        CallPlanHistoryEntry(
            nominal_fire_at=item.nominal_fire_at,
            kind=item.kind,
            result=CallResult.UNKNOWN if item.result is CallResult.PENDING else item.result,
        )
        for item in attempts
    )
    attempt = len(attempts) + 1

    def report(reason: CallPlanReportReason) -> CallPlanStep:
        # Each occurrence fires at its own instant, after the call it reports on.
        return CallPlanStep(
            attempt=attempt,
            kind=CallPlanStepKind.REPORT,
            fire_at=max(now, last.nominal_fire_at + timedelta(seconds=1)),
            reason=reason,
            history=history,
        )

    if last.kind is CallPlanStepKind.FOLLOW_UP:
        return report(
            CallPlanReportReason.FOLLOW_UP_ANSWERED
            if last.result is CallResult.ANSWERED
            else CallPlanReportReason.FOLLOW_UP_NOT_REACHED
        )
    if last.result is CallResult.ANSWERED:
        if plan.follow_up_after_answer_minutes is None:
            return report(CallPlanReportReason.ANSWERED)
        ended = last.ended_at or now
        return CallPlanStep(
            attempt=attempt,
            kind=CallPlanStepKind.FOLLOW_UP,
            fire_at=max(now, ended + timedelta(minutes=plan.follow_up_after_answer_minutes)),
            history=history,
            answered_at=last.nominal_fire_at,
        )
    if last.result is CallResult.NO_CALL:
        return report(CallPlanReportReason.NO_CALL)
    if last.result not in NOT_REACHED:
        return report(CallPlanReportReason.UNKNOWN)
    calls = sum(item.kind in _CALLS for item in attempts)
    if plan.retry_every_minutes is not None and plan.retry_until is not None:
        fire_at = max(now, last.nominal_fire_at + timedelta(minutes=plan.retry_every_minutes))
        if calls < plan.max_calls(first_at) and fire_at <= plan.retry_until:
            return CallPlanStep(
                attempt=attempt, kind=CallPlanStepKind.RETRY, fire_at=fire_at, history=history
            )
    return report(CallPlanReportReason.NOT_REACHED)


def call_plan_note(plan: CallPlan, first_at: datetime, step: CallPlanStep | None) -> str:
    """The platform's line appended to the owner-approved instruction.

    It joins the seed the call authorizer reads for numbers, so it states times
    as hours and minutes and never carries a digit run a number could match.
    """
    total = plan.max_calls(first_at) + (1 if plan.follow_up_after_answer_minutes else 0)
    calls_tail = (
        " Place this one call now. The platform places any retry or follow-up the plan "
        "sets and reports the outcome to the owner, so do not schedule or promise another "
        "call. Leave no voicemail unless the instruction asks for one."
    )
    if step is None:
        return f"Platform note: this is call 1 of up to {total} in a call plan.{calls_tail}"
    history = "; ".join(
        f"{entry.nominal_fire_at:%H:%M} UTC {_WORDS[entry.result]}" for entry in step.history
    )
    if step.kind is CallPlanStepKind.RETRY:
        return (
            f"Platform note: this is call {step.attempt} of up to {total} in a call plan. "
            f"Earlier calls: {history}.{calls_tail}"
        )
    if step.kind is CallPlanStepKind.FOLLOW_UP:
        answered = (
            "an earlier call"
            if step.answered_at is None
            else (f"the call at {step.answered_at:%H:%M} UTC")
        )
        return (
            f"Platform note: this is the follow-up call in a call plan; {answered} was "
            "answered. Place it now, using the instruction's follow-up brief, and have it "
            "open by asking whether they are happy to talk; if the person asks not to be "
            "called again, end the call politely. Do not schedule or promise another call. "
            "Leave no voicemail unless the instruction asks for one."
        )
    assert step.reason is not None
    return (
        "Platform note: the call plan has ended and no further call will be placed. "
        f"Calls: {history}. {_REPORTS[step.reason]} Do not place any call. Tell the owner "
        "the outcome in one or two sentences."
    )
