"""PostgreSQL dispatcher; workers poll the committed queue as the durable signal."""

from __future__ import annotations

from uuid import UUID


class PostgresRunDispatcher:
    async def dispatch(self, run_id: UUID) -> None:
        """Return after commit; the durable queue row is the source of truth."""

        del run_id
