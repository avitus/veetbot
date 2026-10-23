"""Owner-isolated record series storage (Milestone 34)."""

from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.records import (
    Bucket,
    BucketRow,
    BucketStats,
    ComponentSummary,
    Order,
    RecordEntry,
    RecordSeries,
    SeriesSummary,
    ValueAt,
    Window,
)

__all__ = [
    "Bucket",
    "BucketRow",
    "BucketStats",
    "ComponentSummary",
    "Order",
    "RecordStore",
    "SeriesExists",
    "SeriesSummary",
    "ValueAt",
    "Window",
]


class SeriesExists(Exception):  # noqa: N818
    """A series of that name exists with a different declaration."""


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

    async def find_entry(
        self, principal: Principal, series_id: UUID, idempotency_key: str
    ) -> RecordEntry | None: ...

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
