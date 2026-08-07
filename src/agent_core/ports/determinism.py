"""Ports that make wall time and identifier generation explicit."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class IdFactory(Protocol):
    def new_id(self) -> UUID: ...
