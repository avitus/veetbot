"""Instant validation shared by domain records that store aware UTC times."""

from __future__ import annotations

from datetime import UTC, datetime


def aware_utc(value: datetime, subject: str) -> datetime:
    """Normalise an aware instant to UTC; a naive one is a programming error."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{subject} must be aware")
    return value.astimezone(UTC)
