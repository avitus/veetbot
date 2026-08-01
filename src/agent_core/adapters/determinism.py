"""Production and deterministic adapters for time and identifiers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4


class SystemClock:
    """Aware-UTC wall clock used by real entry points."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FixedClock:
    """Controllable aware-UTC clock used by deterministic runs and evals."""

    def __init__(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("FixedClock requires an aware datetime")
        self._current = current.astimezone(UTC)

    def now(self) -> datetime:
        return self._current

    async def sleep(self, seconds: float) -> None:
        self.advance(timedelta(seconds=seconds))
        await asyncio.sleep(0)

    def advance(self, delta: timedelta) -> None:
        self._current += delta


class RandomIdFactory:
    """Production UUID generator isolated behind the IdFactory port."""

    def new_id(self) -> UUID:
        return uuid4()


class SequenceIdFactory:
    """Deterministic UUID sequence with optional authored identifiers."""

    def __init__(self, values: Iterable[UUID] = ()) -> None:
        self._values = iter(values)
        self._counter = 0

    def new_id(self) -> UUID:
        try:
            return next(self._values)
        except StopIteration:
            self._counter += 1
            return UUID(int=self._counter)
