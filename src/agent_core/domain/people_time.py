"""Retain a source calendar zone, or its numeric offset when no zone was named."""

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, Field, ValidationInfo


def preserve_source_timezone(value: str | None, info: ValidationInfo) -> str | None:
    if value is not None:
        if (
            len(value) == 8
            and value[:3] == "GMT"
            and value[3] in "+-"
            and value[4:].isascii()
            and value[4:].isdigit()
            and int(value[4:6]) < 24
            and int(value[6:]) < 60
        ):
            return value
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("source timezone must be a known zone or a GMT offset") from exc
        return value
    for field in ("due_at", "occurred_at", "valid_from", "valid_to"):
        date = info.data.get(field)
        if not isinstance(date, datetime):
            continue
        offset = date.utcoffset()
        if offset is None:
            continue
        seconds = int(offset.total_seconds())
        if seconds % 60:
            # Minute precision is insufficient for this historical timezone.
            raise ValueError("source timezone is required for a sub-minute UTC offset")
        if not seconds:
            return "UTC"
        minutes = abs(seconds) // 60
        return f"GMT{'+' if seconds > 0 else '-'}{minutes // 60:02d}{minutes % 60:02d}"
    return None


SourceTimezone = Annotated[
    str | None,
    Field(min_length=1, max_length=64, validate_default=True),
    AfterValidator(preserve_source_timezone),
]
