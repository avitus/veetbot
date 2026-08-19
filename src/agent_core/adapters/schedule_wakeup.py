"""Best-effort wakeups for the schedule scanner."""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.engine import make_url

SCHEDULE_WAKEUP_CHANNEL = "agent_schedule_wakeup"


class InMemoryScheduleWakeup:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    async def notify(self) -> None:
        self._event.set()

    async def wait(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("schedule wakeup timeout must be positive")
        try:
            await asyncio.wait_for(self._event.wait(), timeout_seconds)
        except TimeoutError:
            return
        finally:
            self._event.clear()

    async def close(self) -> None:
        self._event.set()


class PostgresScheduleWakeup:
    """LISTEN/NOTIFY latency hint; bounded polling remains authoritative."""

    def __init__(self, database_url: str) -> None:
        url = make_url(database_url).set(drivername="postgresql")
        self._dsn = url.render_as_string(hide_password=False)
        self._publisher: asyncpg.Connection[Any] | None = None
        self._publisher_lock = asyncio.Lock()

    async def notify(self) -> None:
        async with self._publisher_lock:
            if self._publisher is None or self._publisher.is_closed():
                self._publisher = await asyncpg.connect(self._dsn)
            await self._publisher.execute(
                "SELECT pg_notify($1, $2)", SCHEDULE_WAKEUP_CHANNEL, "due"
            )

    async def wait(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("schedule wakeup timeout must be positive")
        connection = await asyncpg.connect(self._dsn)
        event = asyncio.Event()

        def listener(
            _connection: asyncpg.Connection[Any],
            _process_id: int,
            _channel: str,
            _payload: str,
        ) -> None:
            event.set()

        listening = False
        try:
            await connection.add_listener(SCHEDULE_WAKEUP_CHANNEL, listener)
            listening = True
            try:
                await asyncio.wait_for(event.wait(), timeout_seconds)
            except TimeoutError:
                return
        finally:
            try:
                if listening:
                    await connection.remove_listener(SCHEDULE_WAKEUP_CHANNEL, listener)
            finally:
                await connection.close()

    async def close(self) -> None:
        async with self._publisher_lock:
            if self._publisher is not None and not self._publisher.is_closed():
                await self._publisher.close()
            self._publisher = None
