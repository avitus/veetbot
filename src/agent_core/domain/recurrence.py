"""Pure recurrence calculation for scheduled runs."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent_core.domain.schedules import Cadence, DailyCadence, OnceCadence, WeeklyCadence


class RecurrenceCalculator:
    """Resolve closed cadence definitions without ambient wall-clock reads."""

    @staticmethod
    def next_after(cadence: Cadence, reference: datetime) -> datetime | None:
        reference_utc = _as_utc(reference)
        if isinstance(cadence, OnceCadence):
            return cadence.at if cadence.at > reference_utc else None

        zone = ZoneInfo(cadence.timezone)
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
        """Count inclusive nominal instants in O(1) time, with at most six day checks."""

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
        weeks, remainder = divmod(day_count, 7)
        return weeks * len(cadence.weekdays) + sum(
            (first_date + timedelta(days=offset)).isoweekday() in cadence.weekdays
            for offset in range(remainder)
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("recurrence reference must be aware")
    return value.astimezone(UTC)


def _matches(cadence: DailyCadence | WeeklyCadence, candidate_date: date) -> bool:
    return isinstance(cadence, DailyCadence) or candidate_date.isoweekday() in cadence.weekdays


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
