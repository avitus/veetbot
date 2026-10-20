"""ADR-0221: the platform runs a call plan from each call's closed result."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_core.application.call_authorization import _PHONE_NUMBER
from agent_core.domain.call_plans import (
    RESULT_WAIT,
    CallPlanAttempt,
    CallPlanReportReason,
    CallPlanStepKind,
    CallResult,
    call_plan_note,
    next_call_plan_step,
)
from agent_core.domain.schedules import CallPlan

FIRST = datetime(2026, 10, 20, 16, 30, tzinfo=UTC)
# The owner's 2026-10-20 request: every 2 minutes until 9:40, check 5 minutes after an answer.
WAKE_UP = CallPlan(
    retry_every_minutes=2,
    retry_until=FIRST + timedelta(minutes=10),
    follow_up_after_answer_minutes=5,
)


def attempt(
    minutes: int,
    result: CallResult,
    kind: CallPlanStepKind = CallPlanStepKind.RETRY,
    *,
    ended_after: float | None = None,
) -> CallPlanAttempt:
    nominal = FIRST + timedelta(minutes=minutes)
    return CallPlanAttempt(
        kind=CallPlanStepKind.FIRST if minutes == 0 and kind is CallPlanStepKind.RETRY else kind,
        nominal_fire_at=nominal,
        materialized_at=nominal,
        result=result,
        ended_at=None if ended_after is None else nominal + timedelta(minutes=ended_after),
    )


def test_a_plan_bounds_its_calls_and_needs_a_retry_window_or_a_follow_up() -> None:
    assert WAKE_UP.max_calls(FIRST) == 6
    assert CallPlan(follow_up_after_answer_minutes=5).max_calls(FIRST) == 1
    with pytest.raises(ValidationError):
        CallPlan()
    with pytest.raises(ValidationError):
        CallPlan(retry_every_minutes=2)
    with pytest.raises(ValidationError):
        CallPlan(retry_until=FIRST + timedelta(minutes=10))
    with pytest.raises(ValidationError):
        CallPlan(retry_every_minutes=0, retry_until=FIRST + timedelta(minutes=10))
    with pytest.raises(ValidationError):
        CallPlan(follow_up_after_answer_minutes=61)
    for window, every in ((0, 2), (61, 10), (10, 1)):
        plan = CallPlan(retry_every_minutes=every, retry_until=FIRST + timedelta(minutes=window))
        with pytest.raises(ValueError):
            plan.validate_for(FIRST)
    CallPlan(retry_every_minutes=6, retry_until=FIRST + timedelta(minutes=54)).validate_for(FIRST)


def test_an_unanswered_call_is_retried_on_the_plans_interval() -> None:
    step = next_call_plan_step(WAKE_UP, FIRST, [attempt(0, CallResult.NO_ANSWER)], FIRST)

    assert step is not None
    assert step.kind is CallPlanStepKind.RETRY
    assert step.attempt == 2
    assert step.fire_at == FIRST + timedelta(minutes=2)


def test_a_late_result_retries_at_once_rather_than_in_the_past() -> None:
    late = FIRST + timedelta(minutes=3)
    step = next_call_plan_step(WAKE_UP, FIRST, [attempt(0, CallResult.VOICEMAIL)], late)

    assert step is not None and step.fire_at == late


def test_a_call_without_a_result_holds_the_plan_then_ends_it_unknown() -> None:
    attempts = [attempt(0, CallResult.PENDING)]
    assert next_call_plan_step(WAKE_UP, FIRST, attempts, FIRST + timedelta(minutes=4)) is None

    step = next_call_plan_step(WAKE_UP, FIRST, attempts, FIRST + RESULT_WAIT)
    assert step is not None
    assert step.kind is CallPlanStepKind.REPORT
    assert step.reason is CallPlanReportReason.UNKNOWN
    assert [entry.result for entry in step.history] == [CallResult.UNKNOWN]


def test_retries_stop_at_the_window_and_the_owner_is_told() -> None:
    attempts = [attempt(minutes, CallResult.NO_ANSWER) for minutes in range(0, 11, 2)]
    now = FIRST + timedelta(minutes=11)
    step = next_call_plan_step(WAKE_UP, FIRST, attempts, now)

    assert step is not None
    assert step.kind is CallPlanStepKind.REPORT
    assert step.reason is CallPlanReportReason.NOT_REACHED
    assert step.attempt == 7
    assert step.fire_at == now


def test_an_answer_stops_retries_and_sets_one_follow_up_after_the_call_ends() -> None:
    attempts = [
        attempt(0, CallResult.NO_ANSWER),
        attempt(2, CallResult.ANSWERED, ended_after=1.5),
    ]
    step = next_call_plan_step(WAKE_UP, FIRST, attempts, FIRST + timedelta(minutes=4))

    assert step is not None
    assert step.kind is CallPlanStepKind.FOLLOW_UP
    assert step.fire_at == FIRST + timedelta(minutes=8, seconds=30)
    assert step.answered_at == FIRST + timedelta(minutes=2)


def test_the_follow_up_may_fall_after_the_retry_window() -> None:
    attempts = [attempt(minutes, CallResult.BUSY) for minutes in range(0, 10, 2)]
    attempts.append(attempt(10, CallResult.ANSWERED, ended_after=1))
    step = next_call_plan_step(WAKE_UP, FIRST, attempts, FIRST + timedelta(minutes=12))

    assert step is not None
    assert step.kind is CallPlanStepKind.FOLLOW_UP
    assert step.fire_at == FIRST + timedelta(minutes=16)


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (CallResult.ANSWERED, CallPlanReportReason.FOLLOW_UP_ANSWERED),
        (CallResult.VOICEMAIL, CallPlanReportReason.FOLLOW_UP_NOT_REACHED),
        (CallResult.NO_CALL, CallPlanReportReason.FOLLOW_UP_NOT_REACHED),
    ],
)
def test_the_follow_up_ends_the_plan_with_a_report(
    result: CallResult, reason: CallPlanReportReason
) -> None:
    attempts = [
        attempt(0, CallResult.ANSWERED, ended_after=1),
        attempt(6, result, CallPlanStepKind.FOLLOW_UP),
    ]
    step = next_call_plan_step(WAKE_UP, FIRST, attempts, FIRST + timedelta(minutes=8))

    assert step is not None
    assert step.kind is CallPlanStepKind.REPORT
    assert step.reason is reason


def test_an_answer_without_a_follow_up_ends_the_plan() -> None:
    plan = CallPlan(retry_every_minutes=2, retry_until=FIRST + timedelta(minutes=10))
    step = next_call_plan_step(
        plan, FIRST, [attempt(0, CallResult.ANSWERED, ended_after=1)], FIRST + timedelta(minutes=2)
    )

    assert step is not None
    assert step.kind is CallPlanStepKind.REPORT
    assert step.reason is CallPlanReportReason.ANSWERED


@pytest.mark.parametrize("result", [CallResult.NO_CALL, CallResult.UNKNOWN])
def test_a_missing_or_unknown_call_never_redials(result: CallResult) -> None:
    """A duplicate call is worse than a missed retry (ADR-0221)."""
    step = next_call_plan_step(WAKE_UP, FIRST, [attempt(0, result)], FIRST + timedelta(minutes=2))

    assert step is not None
    assert step.kind is CallPlanStepKind.REPORT
    assert step.reason is (
        CallPlanReportReason.NO_CALL
        if result is CallResult.NO_CALL
        else CallPlanReportReason.UNKNOWN
    )


def test_a_plan_with_only_a_follow_up_reports_an_unanswered_first_call() -> None:
    plan = CallPlan(follow_up_after_answer_minutes=5)
    step = next_call_plan_step(plan, FIRST, [attempt(0, CallResult.NO_ANSWER)], FIRST)

    assert step is not None
    assert step.kind is CallPlanStepKind.REPORT
    assert step.reason is CallPlanReportReason.NOT_REACHED


def test_notes_name_the_step_and_carry_no_dialable_number() -> None:
    """The note joins the owner-approved seed, so it must never supply a number."""
    attempts = [attempt(minutes, CallResult.NO_ANSWER) for minutes in range(0, 11, 2)]
    retry = next_call_plan_step(WAKE_UP, FIRST, attempts[:1], FIRST)
    report = next_call_plan_step(WAKE_UP, FIRST, attempts, FIRST + timedelta(minutes=11))
    follow_up = next_call_plan_step(
        WAKE_UP, FIRST, [attempt(0, CallResult.ANSWERED, ended_after=1)], FIRST
    )
    assert retry is not None and report is not None and follow_up is not None

    first_note = call_plan_note(WAKE_UP, FIRST, None)
    retry_note = call_plan_note(WAKE_UP, FIRST, retry)
    follow_up_note = call_plan_note(WAKE_UP, FIRST, follow_up)
    report_note = call_plan_note(WAKE_UP, FIRST, report)

    assert "call 1 of up to 7" in first_note
    assert "call 2 of up to 7" in retry_note
    assert "16:30 UTC no answer" in retry_note
    assert "follow-up call" in follow_up_note
    assert "asks not to be called" in follow_up_note
    assert "Do not place any call" in report_note
    assert "16:40 UTC no answer" in report_note
    for note in (first_note, retry_note, follow_up_note, report_note):
        assert note.startswith("Platform note:")
        assert _PHONE_NUMBER.findall(note) == []
