"""The platform feed projector (docs/plan/cockpit.md, platform feeds).

The maintenance role projects each enabled source on a short timer. It
publishes when a source's content changes, and republishes unchanged content
once a refresh period has passed, so a quiet feed stays honestly fresh and a
stopped projector shows as stale. A source that fails reports an error
snapshot without stopping the others, and history older than its window is
pruned while each feed keeps its latest snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck
from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.application.cockpit_projector import CockpitFeedRetention, PlatformFeedProjector
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import CockpitEventKind
from agent_core.ports.persistence import RepositoryUnitOfWork
from tests.contract.support import memory_uow_factory

OWNER = Principal(
    tenant_id="tenant-a", principal_id="owner", scopes={"cockpit.read", "cockpit.write"}
)
SCHEMA = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}


@dataclass
class CountSource:
    feed_id: str
    count: int = 0
    fail: bool = False
    schema: dict[str, Any] = field(default_factory=lambda: dict(SCHEMA))
    reads: int = 0

    async def read(self, uow: RepositoryUnitOfWork, principal: Principal, now: Any) -> Any:
        del uow, principal, now
        self.reads += 1
        if self.fail:
            raise RuntimeError("the source is down; a secret detail lives here")
        return {"count": self.count}


async def _stack(*sources: CountSource, refresh_seconds: int = 300) -> Any:
    clock, uow_factory = await memory_uow_factory()
    feeds = CockpitFeeds(
        uow_factory=uow_factory,
        clock=clock,
        schema_check=Draft202012SchemaCheck(),
        history_max=288,
    )
    projector = PlatformFeedProjector(
        uow_factory=uow_factory,
        clock=clock,
        feeds=feeds,
        principal=OWNER,
        sources=sources,
        refresh_seconds=refresh_seconds,
    )
    return clock, uow_factory, projector


async def _history(uow_factory: Any, feed_id: str) -> list[Any]:
    async with uow_factory() as uow:
        return list(await uow.cockpit_feeds.history(OWNER, feed_id, limit=100))


async def test_the_first_pass_publishes_every_source_with_its_schema_and_interval() -> None:
    approvals, runs = (
        CountSource("veetbot.approvals.pending", 2),
        CountSource("veetbot.runs.activity"),
    )
    _clock, uow_factory, projector = await _stack(approvals, runs)
    assert await projector.run_once() == 2
    async with uow_factory() as uow:
        feed = await uow.cockpit_feeds.get(OWNER, "veetbot.approvals.pending")
        latest = await uow.cockpit_feeds.latest(OWNER, ["veetbot.approvals.pending"])
        events = await uow.cockpit_events.after(OWNER, sequence=0, limit=10)
    assert feed is not None and feed.schema_document == SCHEMA
    snapshot = latest["veetbot.approvals.pending"]
    assert (snapshot.data, snapshot.expected_interval_seconds) == ({"count": 2}, 300)
    assert sorted((event.kind, event.subject) for event in events) == [
        (CockpitEventKind.FEED_UPDATED, "veetbot.approvals.pending"),
        (CockpitEventKind.FEED_UPDATED, "veetbot.runs.activity"),
    ]


async def test_unchanged_content_waits_for_the_refresh_and_a_change_does_not() -> None:
    approvals = CountSource("veetbot.approvals.pending", 1)
    clock, uow_factory, projector = await _stack(approvals, refresh_seconds=300)
    await projector.run_once()
    clock.advance(timedelta(seconds=30))
    assert await projector.run_once() == 0
    approvals.count = 3
    assert await projector.run_once() == 1
    clock.advance(timedelta(seconds=299))
    assert await projector.run_once() == 0
    clock.advance(timedelta(seconds=1))
    assert await projector.run_once() == 1
    history = await _history(uow_factory, "veetbot.approvals.pending")
    assert [item.data for item in history] == [{"count": 1}, {"count": 3}, {"count": 3}]


async def test_a_failing_source_reports_an_error_and_the_others_still_publish() -> None:
    broken, runs = (
        CountSource("veetbot.email.priority", fail=True),
        CountSource("veetbot.runs.activity"),
    )
    clock, uow_factory, projector = await _stack(broken, runs)
    await projector.run_once()
    async with uow_factory() as uow:
        latest = await uow.cockpit_feeds.latest(
            OWNER, ["veetbot.email.priority", "veetbot.runs.activity"]
        )
    assert latest["veetbot.runs.activity"].data == {"count": 0}
    failed = latest["veetbot.email.priority"]
    assert failed.error == "The platform could not read this source."
    assert "secret" not in str(failed.model_dump())
    clock.advance(timedelta(seconds=30))
    await projector.run_once()
    assert len(await _history(uow_factory, "veetbot.email.priority")) == 1
    broken.fail = False
    await projector.run_once()
    recovered = await _history(uow_factory, "veetbot.email.priority")
    assert (recovered[-1].error, recovered[-1].data) == (None, {"count": 0})


async def test_history_beyond_its_window_is_pruned_but_the_latest_stays() -> None:
    approvals = CountSource("veetbot.approvals.pending")
    clock, uow_factory, projector = await _stack(approvals)
    retention = CockpitFeedRetention(
        uow_factory=uow_factory, clock=clock, principal=OWNER, history_days=7
    )
    for count in range(3):
        approvals.count = count
        await projector.run_once()
        clock.advance(timedelta(days=3))
    assert await retention.run_once() == 1
    history = await _history(uow_factory, "veetbot.approvals.pending")
    assert [item.data for item in history] == [{"count": 1}, {"count": 2}]
    clock.advance(timedelta(days=30))
    await retention.run_once()
    assert [item.data for item in await _history(uow_factory, "veetbot.approvals.pending")] == [
        {"count": 2}
    ]
