"""Owner records: named numeric series with one to four components (Milestone 34)."""

from __future__ import annotations

import re
import unicodedata
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


def resolve_observed_at(text: str | None, precision: RecordPrecision, now: datetime) -> datetime:
    if text is None:
        return now
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise RecordTimeError("observed_at is not an ISO 8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecordTimeError("observed_at needs a UTC offset")
    if parsed.astimezone(UTC) > now + FUTURE_SKEW:
        raise RecordTimeError("observed_at is in the future")
    if precision is RecordPrecision.DAY:
        parsed = datetime.combine(parsed.date(), time(12, 0), tzinfo=parsed.tzinfo)
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
