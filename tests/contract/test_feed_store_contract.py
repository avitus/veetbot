"""Feed store contract, shared by the in-memory and PostgreSQL adapters."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import JsonValue

from agent_core.adapters.persistence.cockpit import InMemoryFeedStore
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import Feed, FeedSnapshot, FeedTrust
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.cockpit import FeedStore
from tests.contract.cockpit_support import Scenario, foreign, scenarios
from tests.contract.support import NOW, principal

SCENARIOS, scenario = scenarios()
SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"temperature": {"type": "number"}},
}


def feed(owner: Principal, feed_id: str = "agent.weather") -> Feed:
    return Feed(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        id=feed_id,
        schema=SCHEMA,
        created_at=NOW,
        updated_at=NOW,
    )


def snapshot(
    owner: Principal,
    value: int,
    *,
    feed_id: str = "agent.weather",
    minutes: int = 0,
    schema_version: int = 1,
) -> FeedSnapshot:
    return FeedSnapshot(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        feed_id=feed_id,
        sequence=1,
        observed_at=NOW + timedelta(minutes=minutes),
        expected_interval_seconds=900,
        data={"temperature": value},
        schema_version=schema_version,
        recorded_at=NOW + timedelta(minutes=minutes),
    )


@scenario
async def feeds_register_once_and_stay_private(store: FeedStore, owner: Principal) -> None:
    created, new = await store.register(feed(owner))
    assert new is True
    assert created.trust is FeedTrust.UNTRUSTED
    again, new_again = await store.register(feed(owner).model_copy(update={"schema_document": {}}))
    assert (again, new_again) == (created, False)
    await store.register(feed(owner, "veetbot.approvals.pending"))
    assert [item.id for item in await store.list_feeds(owner)] == [
        "agent.weather",
        "veetbot.approvals.pending",
    ]
    assert await store.get(owner, "agent.weather") == created
    assert await store.get(foreign(owner), "agent.weather") is None
    assert await store.list_feeds(foreign(owner)) == []


@scenario
async def publishing_sequences_snapshots_and_bounds_history(
    store: FeedStore, owner: Principal
) -> None:
    with pytest.raises(NotFoundError):
        await store.publish(snapshot(owner, 1), history_max=3)
    await store.register(feed(owner))
    stored = [
        await store.publish(snapshot(owner, value, minutes=value), history_max=3)
        for value in range(1, 6)
    ]
    assert [item.sequence for item in stored] == [1, 2, 3, 4, 5]
    latest = await store.latest(owner, ["agent.weather", "agent.absent"])
    assert list(latest) == ["agent.weather"]
    assert latest["agent.weather"].data == {"temperature": 5}
    history = await store.history(owner, "agent.weather", limit=10)
    assert [item.sequence for item in history] == [3, 4, 5]
    assert [item.sequence for item in await store.history(owner, "agent.weather", limit=2)] == [
        4,
        5,
    ]
    assert await store.latest(foreign(owner), ["agent.weather"]) == {}
    assert await store.history(foreign(owner), "agent.weather", limit=10) == []


@scenario
async def a_schema_change_refuses_snapshots_of_the_old_shape(
    store: FeedStore, owner: Principal
) -> None:
    await store.register(feed(owner))
    replaced = await store.replace_schema(
        owner, "agent.weather", schema={"type": "object"}, updated_at=NOW + timedelta(minutes=1)
    )
    assert replaced.schema_version == 2
    assert replaced.schema_document == {"type": "object"}
    with pytest.raises(ConflictError) as error:
        await store.publish(snapshot(owner, 1), history_max=3)
    assert error.value.reason == "feed_schema_changed"
    published = await store.publish(snapshot(owner, 1, schema_version=2), history_max=3)
    assert published.schema_version == 2
    with pytest.raises(NotFoundError):
        await store.replace_schema(foreign(owner), "agent.weather", schema={}, updated_at=NOW)


@scenario
async def pruning_keeps_each_feeds_latest_snapshot(store: FeedStore, owner: Principal) -> None:
    await store.register(feed(owner))
    await store.register(feed(owner, "agent.markets"))
    for value in range(1, 4):
        await store.publish(snapshot(owner, value, minutes=value), history_max=10)
    await store.publish(snapshot(owner, 9, feed_id="agent.markets", minutes=1), history_max=10)
    removed = await store.prune_recorded_before(owner, before=NOW + timedelta(minutes=10))
    assert removed == 2
    assert [item.sequence for item in await store.history(owner, "agent.weather", limit=10)] == [3]
    assert (await store.latest(owner, ["agent.markets"]))["agent.markets"].data == {
        "temperature": 9
    }


@scenario
async def erasure_removes_only_the_owners_feeds(store: FeedStore, owner: Principal) -> None:
    await store.register(feed(owner))
    await store.publish(snapshot(owner, 1), history_max=3)
    await store.register(feed(foreign(owner)))
    assert await store.erase_principal(owner) == 2
    assert await store.list_feeds(owner) == []
    assert [item.id for item in await store.list_feeds(foreign(owner))] == ["agent.weather"]


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda check: check.__name__)
async def test_in_memory_feed_store(check: Scenario) -> None:
    await check(InMemoryFeedStore(), principal())
