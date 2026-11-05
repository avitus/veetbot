"""Trials validate real feed schemas without changing the owner's live data."""

from typing import Any

import pytest

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck
from agent_core.application.cockpit_feeds import CockpitFeedRefusedError, CockpitFeeds
from tests.contract.support import memory_uow_factory, principal


async def test_preview_checks_schema_but_does_not_create_or_replace_live_feed() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"cockpit.read", "cockpit.write"}})
    feeds = CockpitFeeds(
        uow_factory=factory, clock=clock, schema_check=Draft202012SchemaCheck(), history_max=10
    )
    schema: dict[str, Any] = {"type": "integer"}
    await feeds.publish(
        owner, "agent.count", data=3, expected_interval_seconds=300, schema=schema, preview=True
    )
    assert await feeds.list_feeds(owner) == []
    await feeds.publish(owner, "agent.count", data=3, expected_interval_seconds=300, schema=schema)
    await feeds.publish(
        owner,
        "agent.count",
        data="preview",
        expected_interval_seconds=300,
        schema={"type": "string"},
        preview=True,
    )
    reading = await feeds.read(owner, "agent.count", history=10)
    assert reading.current is not None and reading.current.data == 3
    assert reading.feed.schema_document == schema
    assert reading.feed.schema_version == 1
    with pytest.raises(CockpitFeedRefusedError):
        await feeds.publish(
            owner, "agent.count", data="invalid", expected_interval_seconds=300, preview=True
        )
    async with factory() as uow:
        assert await uow.cockpit_events.latest_sequence(owner) == 1
