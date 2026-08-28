"""Pure recurrence calculation for scheduled runs."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent_core.domain.schedules import (
    Cadence,
    DailyCadence,
    MonthlyCadence,
    OnceCadence,
    WeeklyCadence,
    YearlyCadence,
)


class RecurrenceCalculator:
    """Resolve closed cadence definitions without ambient wall-clock reads."""

    @staticmethod
    def next_after(cadence: Cadence, reference: datetime) -> datetime | None:
        reference_utc = _as_utc(reference)
        if isinstance(cadence, OnceCadence):
            return cadence.at if cadence.at > reference_utc else None

        zone = ZoneInfo(cadence.timezone)
        if isinstance(cadence, MonthlyCadence):
            return _next_monthly(cadence, reference_utc, zone)
        if isinstance(cadence, YearlyCadence):
            return _next_yearly(cadence, reference_utc, zone)
        local_date = reference_utc.astimezone(zone).date()
        search_days = 2 if isinstance(cadence, DailyCadence) else 8
        for offset in range(search_days):
            candidate_date = local_date + timedelta(days=offset)
            if _matches(cadence, candidate_date):
                candidate = _resolve_civil(candidate_date, cadence.local_time, zone)
                if candidate > reference_utc:
                    return candidate
        return None

    @staticmethod
    def latest_at_or_before(cadence: Cadence, reference: datetime) -> datetime | None:
        reference_utc = _as_utc(reference)
        if isinstance(cadence, OnceCadence):
            return cadence.at if cadence.at <= reference_utc else None

        zone = ZoneInfo(cadence.timezone)
        if isinstance(cadence, MonthlyCadence):
            return _latest_monthly(cadence, reference_utc, zone)
        if isinstance(cadence, YearlyCadence):
            return _latest_yearly(cadence, reference_utc, zone)
        local_date = reference_utc.astimezone(zone).date()
        search_days = 2 if isinstance(cadence, DailyCadence) else 8
        for offset in range(search_days):
            candidate_date = local_date - timedelta(days=offset)
            if _matches(cadence, candidate_date):
                candidate = _resolve_civil(candidate_date, cadence.local_time, zone)
                if candidate <= reference_utc:
                    return candidate
        return None

    @staticmethod
    def count_between(cadence: Cadence, first: datetime, last: datetime) -> int:
        """Count inclusive nominal instants with bounded calendar arithmetic."""

        first_utc = _as_utc(first)
        last_utc = _as_utc(last)
        if last_utc < first_utc:
            return 0
        if isinstance(cadence, OnceCadence):
            return int(first_utc <= cadence.at <= last_utc)
        zone = ZoneInfo(cadence.timezone)
        first_date = first_utc.astimezone(zone).date()
        last_date = last_utc.astimezone(zone).date()
        day_count = (last_date - first_date).days + 1
        if isinstance(cadence, DailyCadence):
            return day_count
        if isinstance(cadence, WeeklyCadence):
            weeks, remainder = divmod(day_count, 7)
            return weeks * len(cadence.weekdays) + sum(
                (first_date + timedelta(days=offset)).isoweekday() in cadence.weekdays
                for offset in range(remainder)
            )
        if isinstance(cadence, MonthlyCadence):
            return _count_monthly(cadence, first_utc, last_utc, zone)
        return _count_yearly(cadence, first_utc, last_utc, zone)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("recurrence reference must be aware")
    return value.astimezone(UTC)


def _matches(cadence: DailyCadence | WeeklyCadence, candidate_date: date) -> bool:
    return isinstance(cadence, DailyCadence) or candidate_date.isoweekday() in cadence.weekdays


def _next_monthly(
    cadence: MonthlyCadence,
    reference: datetime,
    zone: ZoneInfo,
) -> datetime | None:
    local = reference.astimezone(zone)
    for offset in range(24):
        shifted = _shift_month(local.year, local.month, offset)
        if shifted is None:
            return None
        candidates = [
            _resolve_civil(value, cadence.local_time, zone)
            for value in _monthly_dates(cadence, *shifted)
        ]
        eligible = [candidate for candidate in candidates if candidate > reference]
        if eligible:
            return min(eligible)
    return None


def _latest_monthly(
    cadence: MonthlyCadence,
    reference: datetime,
    zone: ZoneInfo,
) -> datetime | None:
    local = reference.astimezone(zone)
    for offset in range(24):
        shifted = _shift_month(local.year, local.month, -offset)
        if shifted is None:
            return None
        candidates = [
            _resolve_civil(value, cadence.local_time, zone)
            for value in _monthly_dates(cadence, *shifted)
        ]
        eligible = [candidate for candidate in candidates if candidate <= reference]
        if eligible:
            return max(eligible)
    return None


def _next_yearly(
    cadence: YearlyCadence,
    reference: datetime,
    zone: ZoneInfo,
) -> datetime | None:
    local_year = reference.astimezone(zone).year
    for offset in range(10):
        year = local_year + offset
        if year > datetime.max.year:
            return None
        candidates = [
            _resolve_civil(value, cadence.local_time, zone)
            for value in _yearly_dates(cadence, year)
        ]
        eligible = [candidate for candidate in candidates if candidate > reference]
        if eligible:
            return min(eligible)
    return None


def _latest_yearly(
    cadence: YearlyCadence,
    reference: datetime,
    zone: ZoneInfo,
) -> datetime | None:
    local_year = reference.astimezone(zone).year
    for offset in range(10):
        year = local_year - offset
        if year < datetime.min.year:
            return None
        candidates = [
            _resolve_civil(value, cadence.local_time, zone)
            for value in _yearly_dates(cadence, year)
        ]
        eligible = [candidate for candidate in candidates if candidate <= reference]
        if eligible:
            return max(eligible)
    return None


def _monthly_dates(cadence: MonthlyCadence, year: int, month: int) -> tuple[date, ...]:
    last_day = monthrange(year, month)[1]
    selected = {day for day in cadence.days_of_month if day <= last_day}
    if cadence.last_day:
        selected.add(last_day)
    return tuple(date(year, month, day) for day in sorted(selected))


def _yearly_dates(cadence: YearlyCadence, year: int) -> tuple[date, ...]:
    values: list[date] = []
    for value in cadence.dates:
        if value.day <= monthrange(year, value.month)[1]:
            values.append(date(year, value.month, value.day))
    return tuple(values)


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int] | None:
    index = (year - 1) * 12 + (month - 1) + offset
    if index < 0 or index >= datetime.max.year * 12:
        return None
    shifted_year, shifted_month = divmod(index, 12)
    return shifted_year + 1, shifted_month + 1


def _count_monthly(
    cadence: MonthlyCadence,
    first: datetime,
    last: datetime,
    zone: ZoneInfo,
) -> int:
    first_date = first.astimezone(zone).date()
    last_date = last.astimezone(zone).date()
    start = _month_index(first_date.year, first_date.month)
    end = _month_index(last_date.year, last_date.month)
    total_months = end - start + 1
    february = _count_month_numbers(start, end, {2})
    leap_february = _count_leap_februaries(start, end)
    thirty_day = _count_month_numbers(start, end, {4, 6, 9, 11})
    thirty_one_day = _count_month_numbers(start, end, {1, 3, 5, 7, 8, 10, 12})

    count = 0
    for day in cadence.days_of_month:
        if day <= 28:
            count += total_months
        elif day == 29:
            count += total_months - (february - leap_february)
        elif day == 30:
            count += total_months - february
        else:
            count += thirty_one_day
    if cadence.last_day:
        count += total_months
        if 28 in cadence.days_of_month:
            count -= february - leap_february
        if 29 in cadence.days_of_month:
            count -= leap_february
        if 30 in cadence.days_of_month:
            count -= thirty_day
        if 31 in cadence.days_of_month:
            count -= thirty_one_day
    return count - _outside_boundary_months(cadence, first, last, zone)


def _count_yearly(
    cadence: YearlyCadence,
    first: datetime,
    last: datetime,
    zone: ZoneInfo,
) -> int:
    first_year = first.astimezone(zone).year
    last_year = last.astimezone(zone).year
    total_years = last_year - first_year + 1
    leap_years = _count_leap_years(first_year, last_year)
    count = sum(
        leap_years if (value.month, value.day) == (2, 29) else total_years
        for value in cadence.dates
    )
    boundary_years = {first_year, last_year}
    for year in boundary_years:
        for candidate_date in _yearly_dates(cadence, year):
            candidate = _resolve_civil(candidate_date, cadence.local_time, zone)
            if candidate < first or candidate > last:
                count -= 1
    return count


def _outside_boundary_months(
    cadence: MonthlyCadence,
    first: datetime,
    last: datetime,
    zone: ZoneInfo,
) -> int:
    first_local = first.astimezone(zone)
    last_local = last.astimezone(zone)
    boundary_months = {
        (first_local.year, first_local.month),
        (last_local.year, last_local.month),
    }
    return sum(
        candidate < first or candidate > last
        for year, month in boundary_months
        for candidate in (
            _resolve_civil(value, cadence.local_time, zone)
            for value in _monthly_dates(cadence, year, month)
        )
    )


def _month_index(year: int, month: int) -> int:
    return (year - 1) * 12 + (month - 1)


def _count_month_numbers(start: int, end: int, months: set[int]) -> int:
    count = 0
    for month in months:
        remainder = month - 1
        first = start + ((remainder - start) % 12)
        if first <= end:
            count += (end - first) // 12 + 1
    return count


def _count_leap_februaries(start: int, end: int) -> int:
    first = start + ((1 - start) % 12)
    if first > end:
        return 0
    last = end - ((end - 1) % 12)
    return _count_leap_years(first // 12 + 1, last // 12 + 1)


def _count_leap_years(first_year: int, last_year: int) -> int:
    if last_year < first_year:
        return 0
    return _leaps_through(last_year) - _leaps_through(first_year - 1)


def _leaps_through(year: int) -> int:
    return year // 4 - year // 100 + year // 400


def _resolve_civil(candidate_date: date, local_time: time, zone: ZoneInfo) -> datetime:
    local = datetime.combine(candidate_date, local_time)
    while local.date() == candidate_date:
        instants = _valid_instants(local, zone)
        if instants:
            return min(instants)
        local += timedelta(seconds=1)
    raise ValueError(f"timezone {zone.key} has no valid instant on {candidate_date.isoformat()}")


def _valid_instants(local: datetime, zone: ZoneInfo) -> set[datetime]:
    instants: set[datetime] = set()
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        round_trip = candidate.astimezone(zone).replace(tzinfo=None)
        if round_trip == local:
            instants.add(candidate)
    return instants
