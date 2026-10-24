"""The Cockpit channel on PostgreSQL wakes streams within the poll (Milestone 35).

docs/plan/cockpit.md, the stream: an event row and its NOTIFY commit together,
the payload carries only the kind and sequence, each tenant has its own
channel, and a waiting stream wakes on the notification instead of its poll.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy.engine import make_url

from agent_core.bootstrap import build
from agent_core.domain.cockpit import CockpitEventKind, cockpit_channel
from agent_core.domain.views import PersistedStreamFrame, StreamFrame
from tests.integration.m2_support import PRINCIPAL, database_settings

OWNER = PRINCIPAL.model_copy(update={"scopes": {"cockpit.read", "cockpit.write"}})
NOW = datetime(2026, 10, 23, 18, 0, tzinfo=UTC)


async def test_an_event_notifies_its_tenant_channel_with_only_kind_and_sequence(
    tmp_path: Path,
) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "a", cockpit_enabled=True)
    dsn = make_url(settings.database_url).set(drivername="postgresql")
    connection = await asyncpg.connect(dsn.render_as_string(hide_password=False))
    received: dict[str, list[dict[str, Any]]] = {"local": [], "other": []}
    try:
        for tenant in received:
            await connection.add_listener(
                cockpit_channel(tenant),
                lambda _c, _p, _ch, payload, tenant=tenant: received[tenant].append(
                    json.loads(payload)
                ),
            )
        async with build(settings=settings, storage="postgres", principal=OWNER) as composition:
            async with composition.uow_factory() as uow:
                event = await uow.cockpit_events.append(
                    OWNER, kind=CockpitEventKind.WIDGET_REVISED, subject="weather", occurred_at=NOW
                )
                await asyncio.sleep(0.2)
                assert received["local"] == []  # Not before the transaction commits.
            for _ in range(50):
                if received["local"]:
                    break
                await asyncio.sleep(0.05)
    finally:
        await connection.close()
    assert received == {"local": [{"kind": "cockpit", "sequence": event.sequence}], "other": []}


async def test_a_waiting_stream_wakes_on_the_notification_not_its_poll(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "a", cockpit_enabled=True)
    async with build(settings=settings, storage="postgres", principal=OWNER) as composition:
        service = composition.services.cockpit
        assert service is not None
        frames = service.stream(OWNER, None).__aiter__()

        async def next_frame() -> StreamFrame:
            return await frames.__anext__()

        first: asyncio.Task[StreamFrame] = asyncio.create_task(next_frame())
        await asyncio.sleep(0.3)
        async with composition.uow_factory() as uow:
            event = await uow.cockpit_events.append(
                OWNER, kind=CockpitEventKind.FEED_UPDATED, subject="agent.weather", occurred_at=NOW
            )
        try:
            frame = await asyncio.wait_for(first, timeout=1.5)
        finally:
            first.cancel()
            await frames.aclose()  # type: ignore[attr-defined]
        assert isinstance(frame, PersistedStreamFrame)
        assert (frame.sequence, frame.event, frame.data) == (
            event.sequence,
            "feed.updated",
            {"subject": "agent.weather"},
        )


@pytest.fixture(autouse=True)
def _short_poll_is_not_used() -> None:
    from agent_core.application import cockpit_views

    assert cockpit_views._IDLE_POLL_SECONDS == 5.0
