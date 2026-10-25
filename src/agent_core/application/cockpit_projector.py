"""The platform feed projector (Milestone 35).

docs/plan/cockpit.md, platform feeds. The maintenance role reads each enabled
source on a short timer and publishes its `veetbot.*` feed when the content
changes. It republishes unchanged content once a refresh period has passed, so
a quiet feed stays honestly fresh and a stopped projector shows as stale. A
source that fails reports a platform-worded error snapshot without stopping
the others, and history older than its window is pruned while each feed
keeps its latest snapshot.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import FeedData, FeedSchemaDocument, FeedSnapshot
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)
SOURCE_FAILED = "The platform could not read this source."


class PlatformFeedSource(Protocol):
    """One `veetbot.*` feed: its schema and how to read it from the principal's state."""

    @property
    def feed_id(self) -> str: ...

    @property
    def schema(self) -> FeedSchemaDocument: ...

    async def read(
        self, uow: RepositoryUnitOfWork, principal: Principal, now: datetime
    ) -> FeedData: ...


def _canonical(data: FeedData) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class PlatformFeedProjector:
    """Publish each source's feed when it changes or its refresh period has passed."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        feeds: CockpitFeeds,
        principal: Principal,
        sources: Sequence[PlatformFeedSource],
        refresh_seconds: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._feeds = feeds
        self._principal = principal
        self._sources = tuple(sources)
        self._refresh = timedelta(seconds=refresh_seconds)
        self._refresh_seconds = refresh_seconds

    async def run_once(self) -> int:
        """Project every source once; return how many feeds were published."""

        published = 0
        for source in self._sources:
            try:
                published += await self._project(source)
            except Exception:
                logger.exception("Cockpit feed projection failed", extra={"feed": source.feed_id})
        return published

    async def _project(self, source: PlatformFeedSource) -> int:
        now = self._clock.now()
        error: str | None = None
        data: FeedData = None
        try:
            async with self._uow_factory() as uow:
                data = await source.read(uow, self._principal, now)
        except Exception:
            # The reason is logged; the feed carries only platform words.
            logger.exception("Cockpit feed source failed", extra={"feed": source.feed_id})
            error = SOURCE_FAILED
        async with self._uow_factory() as uow:
            latest = (await uow.cockpit_feeds.latest(self._principal, [source.feed_id])).get(
                source.feed_id
            )
        if self._current(latest, data, error, now):
            return 0
        await self._feeds.publish(
            self._principal,
            source.feed_id,
            data=data,
            schema=source.schema,
            error=error,
            expected_interval_seconds=self._refresh_seconds,
        )
        return 1

    def _current(
        self, latest: FeedSnapshot | None, data: FeedData, error: str | None, now: datetime
    ) -> bool:
        """Whether the latest snapshot already says this and is inside its refresh period."""

        if latest is None or now - latest.recorded_at >= self._refresh:
            return False
        if error is not None:
            return latest.error is not None
        return latest.error is None and _canonical(latest.data) == _canonical(data)


class CockpitFeedRetention:
    """Drop feed history older than its window, always keeping each feed's latest."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        principal: Principal,
        history_days: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._principal = principal
        self._window = timedelta(days=history_days)

    async def run_once(self) -> int:
        async with self._uow_factory() as uow:
            return await uow.cockpit_feeds.prune_recorded_before(
                self._principal, before=self._clock.now() - self._window
            )
