"""ADR-0254: the INTERVAL cadence a feed-keeping schedule may use.

docs/plan/scheduling.md, cadence and civil time. Its instants are the whole
multiples of `every_minutes` minutes since the Unix epoch, so the phase
depends only on the value, never on creation, revision or daylight saving.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_core.domain.recurrence import RecurrenceCalculator
from agent_core.domain.schedules import (
    FEED_KEEPING_SCOPE,
    CadenceKind,
    DailyCadence,
    IntervalCadence,
    check_interval_cadence,
)
from agent_core.tools.schedule_create import CADENCE_ADAPTER

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("minutes", [1, 15, 1440])
def test_every_minute_value_from_one_to_a_day_is_valid(minutes: int) -> None:
    cadence = IntervalCadence(every_minutes=minutes, timezone="America/Los_Angeles")
    assert cadence.kind is CadenceKind.INTERVAL
    assert CADENCE_ADAPTER.validate_python(cadence.model_dump(mode="json")) == cadence


@pytest.mark.parametrize("minutes", [0, -5, 1441])
def test_an_interval_outside_one_minute_to_one_day_is_refused(minutes: int) -> None:
    with pytest.raises(ValidationError):
        IntervalCadence(every_minutes=minutes, timezone="UTC")


def test_an_interval_keeps_a_real_zone_and_no_local_time() -> None:
    with pytest.raises(ValidationError):
        IntervalCadence(every_minutes=15, timezone="Mars/Olympus_Mons")
    with pytest.raises(ValidationError):
        IntervalCadence.model_validate(
            {"kind": "INTERVAL", "every_minutes": 15, "timezone": "UTC", "local_time": "09:00"}
        )


def test_instants_are_multiples_of_the_interval_since_the_epoch() -> None:
    cadence = IntervalCadence(every_minutes=15, timezone="UTC")
    reference = datetime(2026, 10, 26, 10, 7, 30, tzinfo=UTC)
    assert RecurrenceCalculator.next_after(cadence, reference) == reference.replace(
        minute=15, second=0
    )
    assert RecurrenceCalculator.latest_at_or_before(cadence, reference) == reference.replace(
        minute=0, second=0
    )
    boundary = datetime(2026, 10, 26, 10, 15, tzinfo=UTC)
    assert RecurrenceCalculator.next_after(cadence, boundary) == boundary + timedelta(minutes=15)
    assert RecurrenceCalculator.latest_at_or_before(cadence, boundary) == boundary


def test_an_interval_that_does_not_divide_a_day_is_anchored_at_the_epoch() -> None:
    cadence = IntervalCadence(every_minutes=7, timezone="UTC")
    reference = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    following = RecurrenceCalculator.next_after(cadence, reference)
    assert following is not None
    assert (following - EPOCH) % timedelta(minutes=7) == timedelta(0)
    assert timedelta(0) < following - reference <= timedelta(minutes=7)


def test_the_zone_never_moves_an_interval_across_daylight_saving() -> None:
    hourly = IntervalCadence(every_minutes=60, timezone="America/Los_Angeles")
    # 08:00Z and 09:00Z are both 01:00 in Los Angeles, either side of the fall-back.
    before = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    first = RecurrenceCalculator.next_after(hourly, before)
    assert first is not None
    second = RecurrenceCalculator.next_after(hourly, first)
    third = RecurrenceCalculator.next_after(hourly, second)  # type: ignore[arg-type]
    assert [first, second, third] == [
        datetime(2026, 11, 1, 8, tzinfo=UTC),
        datetime(2026, 11, 1, 9, tzinfo=UTC),
        datetime(2026, 11, 1, 10, tzinfo=UTC),
    ]


def test_a_daily_interval_fires_at_utc_midnight() -> None:
    cadence = IntervalCadence(every_minutes=1440, timezone="Asia/Tokyo")
    reference = datetime(2026, 10, 26, 10, tzinfo=UTC)
    assert RecurrenceCalculator.next_after(cadence, reference) == datetime(2026, 10, 27, tzinfo=UTC)


def test_coalesced_counts_are_inclusive_arithmetic() -> None:
    cadence = IntervalCadence(every_minutes=15, timezone="UTC")
    first = datetime(2026, 10, 26, 10, tzinfo=UTC)
    assert RecurrenceCalculator.count_between(cadence, first, first + timedelta(hours=1)) == 5
    assert RecurrenceCalculator.count_between(cadence, first, first + timedelta(minutes=14)) == 1
    assert RecurrenceCalculator.count_between(cadence, first + timedelta(minutes=1), first) == 0
    every_minute = IntervalCadence(every_minutes=1, timezone="UTC")
    a_year = timedelta(days=365)
    assert RecurrenceCalculator.count_between(every_minute, first, first + a_year) == 525_601


def test_only_a_feed_keeping_schedule_may_use_an_interval() -> None:
    interval = IntervalCadence(every_minutes=5, timezone="UTC")
    daily = DailyCadence.model_validate({"local_time": "09:00", "timezone": "UTC"})
    check_interval_cadence(interval, frozenset({"cockpit.read", FEED_KEEPING_SCOPE}))
    check_interval_cadence(daily, frozenset())
    with pytest.raises(ValueError, match="feed"):
        check_interval_cadence(interval, frozenset({"browser.profile.read"}))
