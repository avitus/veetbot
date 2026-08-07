"""Bounded in-memory and PostgreSQL LISTEN/NOTIFY live transports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.engine import make_url

from agent_core.ports.live_events import LiveNotification, LiveSubscription

LIVE_QUEUE_SIZE = 256
POSTGRES_PAYLOAD_BYTES = 7900


def event_channel(session_id: UUID) -> str:
    return f"agent_session_{session_id.hex}"


class _QueueSubscription:
    def __init__(self, queue: asyncio.Queue[LiveNotification]) -> None:
        self.queue = queue
        self._overflowed = False

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    def offer(self, notification: LiveNotification) -> None:
        try:
            self.queue.put_nowait(notification)
        except asyncio.QueueFull:
            self._overflowed = True

    async def receive(self, timeout_seconds: float) -> LiveNotification | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout_seconds)
        except TimeoutError:
            return None


class InMemoryLiveEventBroadcaster:
    def __init__(self) -> None:
        self._subscriptions: dict[UUID, set[_QueueSubscription]] = {}

    @asynccontextmanager
    async def subscribe(self, session_id: UUID) -> AsyncIterator[LiveSubscription]:
        subscription = _QueueSubscription(asyncio.Queue(maxsize=LIVE_QUEUE_SIZE))
        self._subscriptions.setdefault(session_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            subscribers = self._subscriptions.get(session_id)
            if subscribers is not None:
                subscribers.discard(subscription)
                if not subscribers:
                    self._subscriptions.pop(session_id, None)

    async def publish(
        self,
        session_id: UUID,
        run_id: UUID,
        event: str,
        data: dict[str, Any],
    ) -> None:
        notification = LiveNotification(kind="transient", run_id=run_id, event=event, data=data)
        for subscriber in tuple(self._subscriptions.get(session_id, ())):
            subscriber.offer(notification)

    async def close(self) -> None:
        self._subscriptions.clear()


class PostgresLiveEventBroadcaster:
    def __init__(self, database_url: str) -> None:
        url = make_url(database_url).set(drivername="postgresql")
        self._dsn = url.render_as_string(hide_password=False)
        self._publisher: asyncpg.Connection[Any] | None = None
        self._publisher_lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(self, session_id: UUID) -> AsyncIterator[LiveSubscription]:
        connection = await asyncpg.connect(self._dsn)
        subscription = _QueueSubscription(asyncio.Queue(maxsize=LIVE_QUEUE_SIZE))
        channel = event_channel(session_id)
        listening = False

        def listener(
            _connection: asyncpg.Connection[Any],
            _process_id: int,
            _channel: str,
            payload: str,
        ) -> None:
            try:
                decoded = json.loads(payload)
                notification = LiveNotification(
                    kind=str(decoded["kind"]),
                    run_id=(None if decoded.get("run_id") is None else UUID(decoded["run_id"])),
                    sequence=decoded.get("sequence"),
                    event=decoded.get("event"),
                    data=decoded.get("data"),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return
            subscription.offer(notification)

        try:
            await connection.add_listener(channel, listener)
            listening = True
            yield subscription
        finally:
            try:
                if listening:
                    await connection.remove_listener(channel, listener)
            finally:
                await connection.close()

    async def publish(
        self,
        session_id: UUID,
        run_id: UUID,
        event: str,
        data: dict[str, Any],
    ) -> None:
        payload = json.dumps(
            {
                "kind": "transient",
                "run_id": str(run_id),
                "event": event,
                "data": data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > POSTGRES_PAYLOAD_BYTES:
            return
        async with self._publisher_lock:
            if self._publisher is None or self._publisher.is_closed():
                self._publisher = await asyncpg.connect(self._dsn)
            await self._publisher.execute(
                "SELECT pg_notify($1, $2)", event_channel(session_id), payload
            )

    async def close(self) -> None:
        async with self._publisher_lock:
            if self._publisher is not None and not self._publisher.is_closed():
                await self._publisher.close()
            self._publisher = None
