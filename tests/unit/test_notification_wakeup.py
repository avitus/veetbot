"""Notification wakeup connections are reused and recover after failure."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_core.adapters.notification_wakeup import PostgresNotificationWakeup


class FakeConnection:
    def __init__(self, *, fail_execute: bool = False) -> None:
        self.fail_execute = fail_execute
        self.closed = False
        self.added = 0
        self.removed = 0

    def is_closed(self) -> bool:
        return self.closed

    async def add_listener(self, channel: str, listener: Callable[..., None]) -> None:
        del channel, listener
        self.added += 1

    async def remove_listener(self, channel: str, listener: Callable[..., None]) -> None:
        del channel, listener
        self.removed += 1

    async def execute(self, query: str, channel: str, payload: str) -> None:
        del query, channel, payload
        if self.fail_execute:
            raise OSError("publisher failed")

    async def close(self) -> None:
        self.closed = True


async def test_postgres_notification_wakeup_reuses_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    connections: list[FakeConnection] = []

    async def connect(_dsn: str) -> FakeConnection:
        connections.append(connection)
        return connection

    monkeypatch.setattr("agent_core.adapters.notification_wakeup.asyncpg.connect", connect)
    wakeup = PostgresNotificationWakeup("postgresql://localhost/test")

    await wakeup.wait(0.001)
    await wakeup.wait(0.001)
    await wakeup.close()

    assert connections == [connection]
    assert connection.added == 1
    assert connection.removed == 1


async def test_postgres_notification_wakeup_drops_failed_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(fail_execute=True)

    async def connect(_dsn: str) -> FakeConnection:
        return connection

    monkeypatch.setattr("agent_core.adapters.notification_wakeup.asyncpg.connect", connect)
    wakeup = PostgresNotificationWakeup("postgresql://localhost/test")

    with pytest.raises(OSError, match="publisher failed"):
        await wakeup.notify()

    assert connection.closed
