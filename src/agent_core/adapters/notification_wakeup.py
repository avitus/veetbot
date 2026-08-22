"""Best-effort wakeups for the notification dispatcher."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.engine import make_url

NOTIFICATION_WAKEUP_CHANNEL = "agent_notification_wakeup"


class InMemoryNotificationWakeup:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    async def notify(self) -> None:
        self._event.set()

    async def wait(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("notification wakeup timeout must be positive")
        try:
            await asyncio.wait_for(self._event.wait(), timeout_seconds)
        except TimeoutError:
            return
        self._event.clear()

    async def close(self) -> None:
        self._event.set()


class PostgresNotificationWakeup:
    """LISTEN/NOTIFY latency hint; bounded polling remains authoritative."""

    def __init__(self, database_url: str) -> None:
        url = make_url(database_url).set(drivername="postgresql")
        self._dsn = url.render_as_string(hide_password=False)
        self._publisher: asyncpg.Connection[Any] | None = None
        self._publisher_lock = asyncio.Lock()
        self._listener: asyncpg.Connection[Any] | None = None
        self._listener_lock = asyncio.Lock()
        self._wait_lock = asyncio.Lock()
        self._event = asyncio.Event()

        def receive(
            _connection: asyncpg.Connection[Any],
            _process_id: int,
            _channel: str,
            _payload: str,
        ) -> None:
            self._event.set()

        self._receive = receive

    async def notify(self) -> None:
        async with self._publisher_lock:
            if self._publisher is None or self._publisher.is_closed():
                self._publisher = await asyncpg.connect(self._dsn)
            try:
                await self._publisher.execute(
                    "SELECT pg_notify($1, $2)", NOTIFICATION_WAKEUP_CHANNEL, "due"
                )
            except Exception:
                with suppress(Exception):
                    await self._publisher.close()
                self._publisher = None
                raise

    async def wait(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("notification wakeup timeout must be positive")
        async with self._wait_lock:
            await self._ensure_listener()
            try:
                await asyncio.wait_for(self._event.wait(), timeout_seconds)
            except TimeoutError:
                return
            self._event.clear()

    async def _ensure_listener(self) -> None:
        async with self._listener_lock:
            if self._listener is not None and not self._listener.is_closed():
                return
            connection = await asyncpg.connect(self._dsn)
            try:
                await connection.add_listener(NOTIFICATION_WAKEUP_CHANNEL, self._receive)
            except Exception:
                with suppress(Exception):
                    await connection.close()
                raise
            self._listener = connection

    async def close(self) -> None:
        self._event.set()
        async with self._listener_lock:
            if self._listener is not None and not self._listener.is_closed():
                with suppress(Exception):
                    await self._listener.remove_listener(
                        NOTIFICATION_WAKEUP_CHANNEL,
                        self._receive,
                    )
                with suppress(Exception):
                    await self._listener.close()
            self._listener = None
        async with self._publisher_lock:
            if self._publisher is not None and not self._publisher.is_closed():
                with suppress(Exception):
                    await self._publisher.close()
            self._publisher = None
