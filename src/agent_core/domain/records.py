"""Owner records: named numeric series with one to four components (Milestone 34)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity

MAX_SERIES_PER_PRINCIPAL: Final = 64
MAX_ENTRIES_PER_SERIES: Final = 100_000
NOTE_MAX_CHARACTERS: Final = 200
FUTURE_SKEW = timedelta(minutes=5)

_NAME_MAX: Final = 64
_COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MAX_INTEGER_DIGITS: Final = 12
_MAX_FRACTION_DIGITS: Final = 6
_SIX_PLACES = Decimal("0.000001")


class RecordCadence(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RecordPrecision(StrEnum):
    EXACT = "exact"
    DAY = "day"


class RecordValueError(ValueError):
    """A value is not finite or exceeds twelve integer or six fraction digits."""


class RecordTimeError(ValueError):
    """An observation time is unparsable, lacks an offset, or lies in the future."""


class RecordFutureError(RecordTimeError):
    """An observation time lies beyond the clock plus the permitted skew."""


class RecordNameError(ValueError):
    """A series name is empty, too long, or has no letter or digit."""


def normalize_series_name(display_name: str) -> str:
    name = " ".join(unicodedata.normalize("NFC", display_name).casefold().split())
    if not name or len(name) > _NAME_MAX or not any(ch.isalnum() for ch in name):
        raise RecordNameError("series name must be 1-64 characters with a letter or digit")
    return name


def _quantized(value: Decimal) -> Decimal:
    quantized = value.quantize(_SIX_PLACES)
    return abs(quantized) if quantized == 0 else quantized


def validate_value(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise RecordValueError("value must be finite")
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    if any(digits):
        coefficient = "".join(map(str, digits)).rstrip("0")
        trailing = len(digits) - len(coefficient)
        if exponent + trailing < -_MAX_FRACTION_DIGITS:
            raise RecordValueError("value has more than six fraction digits")
        if value.adjusted() + 1 > _MAX_INTEGER_DIGITS:
            raise RecordValueError("value has more than twelve integer digits")
    return _quantized(value)


def canonical_value(value: Decimal) -> str:
    return f"{_quantized(value):f}"


def short_value(value: Decimal) -> str:
    text = canonical_value(value)
    return text.rstrip("0").rstrip(".")


def _aware(text: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise RecordTimeError(f"{field} is not an ISO 8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecordTimeError(f"{field} needs a UTC offset")
    return parsed


def parse_instant(text: str) -> datetime:
    """An aware ISO 8601 instant in UTC, with no future bound (query window edges)."""

    return _aware(text, "instant").astimezone(UTC)


def resolve_observed_at(text: str | None, precision: RecordPrecision, now: datetime) -> datetime:
    """The stored instant of an append; never later than ``now`` for a day-precision entry.

    The future refusal applies to the instant the owner gave. A day-precision
    entry then moves to noon of its civil date in the owner's offset, capped
    at the clock, so today's entry is never stored in the future.
    """

    if text is None:
        return now
    parsed = _aware(text, "observed_at")
    if parsed.astimezone(UTC) > now + FUTURE_SKEW:
        raise RecordFutureError("observed_at is in the future")
    if precision is RecordPrecision.DAY:
        noon = datetime.combine(parsed.date(), time(12, 0), tzinfo=parsed.tzinfo)
        midnight = datetime.combine(parsed.date(), time(0, 0), tzinfo=parsed.tzinfo)
        return max(midnight.astimezone(UTC), min(noon.astimezone(UTC), now.astimezone(UTC)))
    return parsed.astimezone(UTC)


_INTERVALS = {
    RecordCadence.DAILY: timedelta(days=1),
    RecordCadence.WEEKLY: timedelta(days=7),
    RecordCadence.MONTHLY: timedelta(days=30),
}


def cadence_interval(cadence: RecordCadence) -> timedelta:
    return _INTERVALS[cadence]


def due_since(
    cadence: RecordCadence | None, last_observed_at: datetime | None, now: datetime
) -> datetime | None:
    if cadence is None:
        return None
    if last_observed_at is None:
        return now
    due = last_observed_at + cadence_interval(cadence)
    return due if now >= due else None


class RecordValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecordComponent(RecordValue):
    name: str
    unit: str | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        if not _COMPONENT_NAME.fullmatch(name):
            raise ValueError("component name must match ^[a-z][a-z0-9_]{0,31}$")
        return name

    @field_validator("unit")
    @classmethod
    def valid_unit(cls, unit: str | None) -> str | None:
        if unit is None:
            return None
        trimmed = unit.strip()
        if not trimmed:
            return None
        if len(trimmed) > 16:
            raise ValueError("unit is at most 16 characters")
        return trimmed


class RecordSeries(RecordValue):
    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=_NAME_MAX)
    components: tuple[RecordComponent, ...] = Field(min_length=1, max_length=4)
    sensitivity: Sensitivity = Sensitivity.SENSITIVE
    cadence: RecordCadence | None = None
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def consistent(self) -> RecordSeries:
        names = [component.name for component in self.components]
        if len(set(names)) != len(names):
            raise ValueError("component names must be unique")
        if SENSITIVITY_ORDER[self.sensitivity] < SENSITIVITY_ORDER[Sensitivity.INTERNAL]:
            raise ValueError("series sensitivity must be at least internal")
        if self.updated_at < self.created_at:
            raise ValueError("series update precedes creation")
        return self


def ordered_values(series: RecordSeries, values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """``values`` in the series' declared component order, whatever order a store returned."""

    return {c.name: values[c.name] for c in series.components if c.name in values}


class RecordEntry(RecordValue):
    id: UUID
    series_id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    observed_precision: RecordPrecision = RecordPrecision.EXACT
    values: dict[str, Decimal] = Field(min_length=1, max_length=4)
    note: str | None = None
    note_withheld: bool = False
    idempotency_key: str = Field(min_length=1)
    source_session_id: UUID | None = None
    source_event_id: UUID | None = None
    created_at: AwareDatetime

    @field_validator("observed_at")
    @classmethod
    def to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("values")
    @classmethod
    def valid_values(cls, values: dict[str, Decimal]) -> dict[str, Decimal]:
        for key in values:
            if not _COMPONENT_NAME.fullmatch(key):
                raise ValueError("value key must be a component name")
        try:
            return {key: validate_value(value) for key, value in values.items()}
        except RecordValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("note")
    @classmethod
    def valid_note(cls, note: str | None) -> str | None:
        if note is None:
            return None
        trimmed = note.strip()
        if len(trimmed) > NOTE_MAX_CHARACTERS:
            raise ValueError("note is at most 200 characters")
        return trimmed or None


class Order(StrEnum):
    ASC = "asc"
    DESC = "desc"


class Bucket(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Window(_Result):
    """A half-open interval ``[since, until)``; an absent ``since`` is unbounded."""

    since: AwareDatetime | None = None
    until: AwareDatetime


class SeriesSummary(_Result):
    series: RecordSeries
    entry_count: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None


class ValueAt(_Result):
    value: Decimal
    observed_at: datetime


class ComponentSummary(_Result):
    count: int
    minimum: Decimal
    maximum: Decimal
    mean: Decimal
    first: ValueAt
    last: ValueAt


class BucketStats(_Result):
    count: int
    minimum: Decimal
    maximum: Decimal
    mean: Decimal


class BucketRow(_Result):
    start: datetime
    end: datetime
    components: dict[str, BucketStats]


class AppendResult(_Result):
    entry: RecordEntry
    series: RecordSeries
    entry_count: int
    replayed: bool


class RecordDraft(_Result):
    """One entry as the caller states it, before the service validates it."""

    values: dict[str, Decimal]
    observed_at: str | None
    precision: RecordPrecision
    note: str | None


class ImportResult(_Result):
    series: RecordSeries
    entries: list[RecordEntry]
    entry_count: int
    imported: int
    replayed: int


class QueryResult(_Result):
    series: RecordSeries
    window: Window
    entries: list[RecordEntry]
    summary: dict[str, ComponentSummary]
    buckets: list[BucketRow] | None


class DeleteResult(_Result):
    series: RecordSeries
    entries_removed: int
    series_removed: bool


class CatalogLine(_Result):
    display_name: str
    components: tuple[RecordComponent, ...]
    entry_count: int
    last_observed_at: datetime | None
    last_precision: RecordPrecision | None
    cadence: RecordCadence | None
    due_since: datetime | None


class RecordsCatalog(_Result):
    lines: tuple[CatalogLine, ...]
    omitted: int
