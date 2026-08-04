"""Production and deterministic adapters for time and identifiers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from agent_core.ports.determinism import Clock, IdFactory


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


class UUID7RequestIdFactory:
    """Produce RFC 9562 UUIDv7 request identifiers from injected sources."""

    def __init__(self, clock: Clock, random_ids: IdFactory) -> None:
        self._clock = clock
        self._random_ids = random_ids

    def new_id(self) -> UUID:
        milliseconds = int(self._clock.now().timestamp() * 1000) & ((1 << 48) - 1)
        random_bits = self._random_ids.new_id().int & ((1 << 80) - 1)
        value = (milliseconds << 80) | random_bits
        value &= ~(0xF << 76)
        value |= 0x7 << 76
        value &= ~(0b11 << 62)
        value |= 0b10 << 62
        return UUID(int=value)


class SequenceIdFactory:
    """Deterministic UUID sequence with optional authored identifiers."""

    def __init__(self, values: Iterable[UUID] = ()) -> None:
        self._values = iter(values)
        self._counter = 0
        self._issued: set[UUID] = set()

    def new_id(self) -> UUID:
        try:
            value = next(self._values)
        except StopIteration:
            while True:
                self._counter += 1
                value = UUID(int=self._counter)
                if value not in self._issued:
                    break
        if value in self._issued:
            raise ValueError("SequenceIdFactory authored values must be unique")
        self._issued.add(value)
        return value
