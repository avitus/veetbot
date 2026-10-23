"""Owner-isolated record series storage (Milestone 34)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict

from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.records import RecordEntry, RecordSeries


class Order(StrEnum):
    ASC = "asc"
    DESC = "desc"


class Bucket(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class SeriesExists(Exception):  # noqa: N818
    """A series of that name exists with a different declaration."""


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


class RecordStore(Protocol):
    async def create_series(self, series: RecordSeries) -> tuple[RecordSeries, bool]:
        """Return ``(new, True)``, ``(existing, False)`` for an equal declaration, or raise."""
        ...

    async def get_series(self, principal: Principal, name: str) -> RecordSeries | None: ...

    async def count_series(self, principal: Principal) -> int: ...

    async def list_series(
        self, principal: Principal, ceiling: Sensitivity
    ) -> list[SeriesSummary]: ...

    async def append(self, entry: RecordEntry) -> tuple[RecordEntry, bool]:
        """Insert, or return ``(stored, True)`` when the series holds the idempotency key."""
        ...

    async def count_entries(self, principal: Principal, series_id: UUID) -> int: ...

    async def query(
        self,
        principal: Principal,
        series_id: UUID,
        window: Window,
        order: Order,
        limit: int,
    ) -> list[RecordEntry]: ...

    async def summarize(
        self, principal: Principal, series_id: UUID, window: Window
    ) -> dict[str, ComponentSummary]: ...

    async def bucket(
        self,
        principal: Principal,
        series_id: UUID,
        window: Window,
        bucket: Bucket,
        zone: ZoneInfo,
    ) -> list[BucketRow]: ...

    async def delete_entry(self, principal: Principal, series_id: UUID, entry_id: UUID) -> int: ...

    async def delete_series(self, principal: Principal, series_id: UUID) -> tuple[int, bool]: ...

    async def detach_session(self, session_id: UUID) -> int:
        """Null the session and event provenance of its entries, keeping the rows."""
        ...

    async def erase_principal(self, principal: Principal) -> int: ...
