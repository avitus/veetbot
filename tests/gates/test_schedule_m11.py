"""Milestone 11 schedule recurrence and civil-time gates."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_core.domain.recurrence import RecurrenceCalculator
from agent_core.domain.runs import RunLimits
from agent_core.domain.schedules import (
    DailyCadence,
    OccurrenceDisposition,
    OnceCadence,
    Schedule,
    ScheduleOccurrence,
    SchedulePauseReason,
    ScheduleRevision,
    ScheduleState,
    WeeklyCadence,
)

SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000000111")
AGENT_ID = UUID("00000000-0000-0000-0000-000000000112")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000113")
RUN_ID = UUID("00000000-0000-0000-0000-000000000114")


def _schedule(**updates: object) -> Schedule:
    values: dict[str, object] = {
        "id": SCHEDULE_ID,
        "tenant_id": "tenant-a",
        "principal_id": "principal-a",
        "state": ScheduleState.ACTIVE,
        "current_revision": 1,
        "next_fire_at": datetime(2026, 8, 20, 16, tzinfo=UTC),
        "created_at": datetime(2026, 8, 19, 16, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 19, 16, tzinfo=UTC),
    }
    values.update(updates)
    return Schedule.model_validate(values)


def _revision(**updates: object) -> ScheduleRevision:
    values: dict[str, object] = {
        "schedule_id": SCHEDULE_ID,
        "revision": 1,
        "title": "Daily briefing",
        "instruction": "Summarize today's project changes.",
        "agent_id": AGENT_ID,
        "agent_version": "1.0.0",
        "policy_profile": "default",
        "requested_scopes": frozenset({"memory.read"}),
        "limits": RunLimits(
            max_steps=8,
            max_model_calls=8,
            max_tool_calls=12,
            max_cost=Decimal("2.50"),
        ),
        "run_timeout_seconds": 300,
        "cadence": DailyCadence(local_time=time(9), timezone="America/Los_Angeles"),
        "timezone": "America/Los_Angeles",
        "misfire_grace_seconds": 600,
        "max_consecutive_failures": 3,
        "created_by_principal_id": "principal-a",
        "created_at": datetime(2026, 8, 19, 16, tzinfo=UTC),
    }
    values.update(updates)
    return ScheduleRevision.model_validate(values)


def test_once_cadence_requires_an_aware_instant_and_normalizes_to_utc() -> None:
    cadence = OnceCadence.model_validate({"at": "2026-08-20T09:00:00-07:00"})

    assert cadence.at == datetime(2026, 8, 20, 16, tzinfo=UTC)
    assert RecurrenceCalculator.next_after(
        cadence, datetime(2026, 8, 20, 15, 59, 59, tzinfo=UTC)
    ) == datetime(2026, 8, 20, 16, tzinfo=UTC)
    assert RecurrenceCalculator.next_after(cadence, cadence.at) is None

    with pytest.raises(ValidationError, match="aware"):
        OnceCadence(at=datetime(2026, 8, 20, 16))


def test_daily_spring_gap_advances_to_first_valid_local_instant() -> None:
    cadence = DailyCadence(local_time=time(2, 30), timezone="America/Los_Angeles")

    occurrence = RecurrenceCalculator.next_after(
        cadence, datetime(2026, 3, 8, 9, 59, 59, tzinfo=UTC)
    )

    assert occurrence == datetime(2026, 3, 8, 10, tzinfo=UTC)
    assert occurrence.astimezone(ZoneInfo(cadence.timezone)).time() == time(3)


def test_daily_fall_fold_chooses_the_earlier_instant_once() -> None:
    cadence = DailyCadence(local_time=time(1, 30), timezone="America/Los_Angeles")
    earlier_fold = datetime(2026, 11, 1, 8, 30, tzinfo=UTC)

    assert (
        RecurrenceCalculator.next_after(cadence, datetime(2026, 11, 1, 8, 29, 59, tzinfo=UTC))
        == earlier_fold
    )
    assert RecurrenceCalculator.next_after(cadence, earlier_fold) == datetime(
        2026, 11, 2, 9, 30, tzinfo=UTC
    )


def test_daily_recurrence_uses_civil_time_instead_of_utc_duration() -> None:
    cadence = DailyCadence(local_time=time(9), timezone="America/Los_Angeles")

    before_transition = RecurrenceCalculator.next_after(
        cadence, datetime(2026, 3, 7, 16, 59, tzinfo=UTC)
    )
    assert before_transition is not None
    after_transition = RecurrenceCalculator.next_after(cadence, before_transition)

    assert before_transition == datetime(2026, 3, 7, 17, tzinfo=UTC)
    assert after_transition == datetime(2026, 3, 8, 16, tzinfo=UTC)
    assert after_transition - before_transition == timedelta(hours=23)


def test_weekly_cadence_normalizes_weekdays_and_rejects_invalid_definitions() -> None:
    cadence = WeeklyCadence(
        local_time=time(9),
        weekdays=(5, 1, 3),
        timezone="Europe/London",
    )

    assert cadence.weekdays == (1, 3, 5)
    assert RecurrenceCalculator.next_after(
        cadence, datetime(2026, 8, 18, 12, tzinfo=UTC)
    ) == datetime(2026, 8, 19, 8, tzinfo=UTC)

    with pytest.raises(ValidationError, match="unique"):
        WeeklyCadence(local_time=time(9), weekdays=(1, 1), timezone="UTC")
    with pytest.raises(ValidationError, match="1 through 7"):
        WeeklyCadence(local_time=time(9), weekdays=(0,), timezone="UTC")
    with pytest.raises(ValidationError, match="IANA"):
        DailyCadence(local_time=time(9), timezone="Mars/Olympus_Mons")
    with pytest.raises(ValidationError, match="whole-second"):
        DailyCadence(local_time=time(9, microsecond=1), timezone="UTC")


def test_latest_due_lookup_is_direct_across_long_downtime() -> None:
    cadence = DailyCadence(local_time=time(9), timezone="America/Los_Angeles")
    now = datetime(2046, 8, 19, 20, tzinfo=UTC)

    assert RecurrenceCalculator.latest_at_or_before(cadence, now) == datetime(
        2046, 8, 19, 16, tzinfo=UTC
    )
    assert RecurrenceCalculator.next_after(cadence, now) == datetime(2046, 8, 20, 16, tzinfo=UTC)


@given(
    reference=st.datetimes(
        min_value=datetime(2025, 1, 1),
        max_value=datetime(2035, 12, 31, 23, 59, 59),
        timezones=st.just(UTC),
    ),
    hour=st.integers(min_value=0, max_value=23),
    minute=st.integers(min_value=0, max_value=59),
    timezone=st.sampled_from(
        ["UTC", "America/Los_Angeles", "Europe/London", "Asia/Kathmandu", "Australia/Lord_Howe"]
    ),
    weekly_weekday=st.one_of(st.none(), st.integers(min_value=1, max_value=7)),
)
def _check_generated_civil_time_recurrence(
    reference: datetime,
    hour: int,
    minute: int,
    timezone: str,
    weekly_weekday: int | None,
) -> None:
    cadence = (
        DailyCadence(local_time=time(hour, minute), timezone=timezone)
        if weekly_weekday is None
        else WeeklyCadence(
            local_time=time(hour, minute),
            weekdays=(weekly_weekday,),
            timezone=timezone,
        )
    )

    first = RecurrenceCalculator.next_after(cadence, reference)
    reconstructed = RecurrenceCalculator.next_after(
        type(cadence).model_validate(cadence.model_dump()), reference
    )

    assert first == reconstructed
    assert first is not None
    assert first > reference
    assert first.tzinfo is UTC
    assert RecurrenceCalculator.latest_at_or_before(cadence, first) == first


def test_civil_time_recurrence_is_deterministic() -> None:
    """Run the complete example and generated contract behind the registered gate."""

    test_daily_spring_gap_advances_to_first_valid_local_instant()
    test_daily_fall_fold_chooses_the_earlier_instant_once()
    test_daily_recurrence_uses_civil_time_instead_of_utc_duration()
    test_weekly_cadence_normalizes_weekdays_and_rejects_invalid_definitions()
    _check_generated_civil_time_recurrence()


@given(
    reference=st.datetimes(
        min_value=datetime(2025, 1, 1),
        max_value=datetime(2035, 12, 31, 23, 59, 59),
        timezones=st.just(UTC),
    ),
    delay_seconds=st.integers(min_value=1, max_value=31_536_000),
)
def _check_generated_one_time_occurrence_is_not_early(
    reference: datetime, delay_seconds: int
) -> None:
    cadence = OnceCadence(at=reference + timedelta(seconds=delay_seconds))
    nominal = RecurrenceCalculator.next_after(cadence, reference)
    assert nominal is not None
    assert nominal > reference


def test_future_occurrences_are_never_early() -> None:
    _check_generated_civil_time_recurrence()
    _check_generated_one_time_occurrence_is_not_early()


def test_schedule_state_and_cached_instant_are_consistent() -> None:
    paused = _schedule(state=ScheduleState.PAUSED, pause_reason=SchedulePauseReason.USER)
    assert paused.pause_reason is SchedulePauseReason.USER

    with pytest.raises(ValidationError, match="paused schedule requires"):
        _schedule(state=ScheduleState.PAUSED)
    with pytest.raises(ValidationError, match="only a paused schedule"):
        _schedule(pause_reason=SchedulePauseReason.FAILURE_LIMIT)
    with pytest.raises(ValidationError, match="terminal schedule"):
        _schedule(state=ScheduleState.CANCELLED)
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        _schedule(current_revision=0)
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        _schedule(consecutive_failures=-1)


@pytest.mark.parametrize(
    "limits",
    [
        RunLimits(max_steps=0, max_model_calls=1, max_tool_calls=1, max_cost=Decimal("1")),
        RunLimits(max_steps=1, max_model_calls=0, max_tool_calls=1, max_cost=Decimal("1")),
        RunLimits(max_steps=1, max_model_calls=1, max_tool_calls=0, max_cost=Decimal("1")),
        RunLimits(max_steps=1, max_model_calls=1, max_tool_calls=1),
        RunLimits(max_steps=1, max_model_calls=1, max_tool_calls=1, max_cost=Decimal("0")),
    ],
)
def test_schedule_revision_requires_finite_positive_limits(limits: RunLimits) -> None:
    with pytest.raises(ValidationError, match="finite positive"):
        _revision(limits=limits)


def test_schedule_revision_pins_cadence_timezone_and_bounds() -> None:
    assert _revision().requested_scopes == frozenset({"memory.read"})

    with pytest.raises(ValidationError, match="cadence timezone"):
        _revision(timezone="UTC")
    with pytest.raises(ValidationError, match="one-time cadence has no timezone"):
        _revision(
            cadence=OnceCadence(at=datetime(2026, 8, 20, 16, tzinfo=UTC)),
            timezone="UTC",
        )
    for field in ("run_timeout_seconds", "misfire_grace_seconds", "max_consecutive_failures"):
        with pytest.raises(ValidationError, match="greater than 0"):
            _revision(**{field: 0})


def test_occurrence_disposition_controls_links_and_reason() -> None:
    materialized = ScheduleOccurrence(
        id=UUID(int=1),
        schedule_id=SCHEDULE_ID,
        schedule_revision=1,
        nominal_fire_at=datetime(2026, 8, 20, 16, tzinfo=UTC),
        disposition=OccurrenceDisposition.MATERIALIZED,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        authority_version="authority-7",
        materialized_at=datetime(2026, 8, 20, 16, 0, 1, tzinfo=UTC),
        created_at=datetime(2026, 8, 20, 16, 0, 1, tzinfo=UTC),
    )
    assert materialized.run_id == RUN_ID

    erased_at = datetime(2026, 8, 21, 16, tzinfo=UTC)
    erased = ScheduleOccurrence.model_validate(
        materialized.model_dump()
        | {"session_id": None, "run_id": None, "links_erased_at": erased_at}
    )
    assert erased.links_erased_at == erased_at

    with pytest.raises(ValidationError, match="erased materialized occurrence"):
        ScheduleOccurrence.model_validate(
            materialized.model_dump() | {"session_id": None, "links_erased_at": erased_at}
        )

    with pytest.raises(ValidationError, match="materialized occurrence requires"):
        ScheduleOccurrence.model_validate(materialized.model_dump() | {"run_id": None})
    with pytest.raises(ValidationError, match="cannot precede"):
        ScheduleOccurrence.model_validate(
            materialized.model_dump()
            | {"materialized_at": datetime(2026, 8, 20, 15, 59, 59, tzinfo=UTC)}
        )
    with pytest.raises(ValidationError, match="non-materialized occurrence"):
        ScheduleOccurrence.model_validate(
            materialized.model_dump()
            | {
                "disposition": OccurrenceDisposition.MISSED,
                "reason_code": "schedule.grace_expired",
                "links_erased_at": None,
            }
        )
    with pytest.raises(ValidationError, match="stable reason"):
        ScheduleOccurrence.model_validate(
            materialized.model_dump()
            | {
                "disposition": OccurrenceDisposition.MISSED,
                "session_id": None,
                "run_id": None,
                "authority_version": None,
                "materialized_at": None,
            }
        )


@given(
    state=st.sampled_from(list(ScheduleState)),
    pause_reason=st.one_of(st.none(), st.sampled_from(list(SchedulePauseReason))),
    has_next_fire=st.booleans(),
)
def _check_generated_schedule_state_is_total(
    state: ScheduleState,
    pause_reason: SchedulePauseReason | None,
    has_next_fire: bool,
) -> None:
    values = {
        "state": state,
        "pause_reason": pause_reason,
        "next_fire_at": datetime(2026, 8, 20, 16, tzinfo=UTC) if has_next_fire else None,
    }
    legal = (state is ScheduleState.PAUSED) == (pause_reason is not None) and not (
        state in {ScheduleState.COMPLETED, ScheduleState.CANCELLED} and has_next_fire
    )

    if legal:
        assert _schedule(**values).state is state
    else:
        with pytest.raises(ValidationError):
            _schedule(**values)


def test_schedule_domain_validation_is_total() -> None:
    """Exercise generated state combinations and every cross-field contract."""

    _check_generated_schedule_state_is_total()
    test_schedule_revision_pins_cadence_timezone_and_bounds()
    test_occurrence_disposition_controls_links_and_reason()
