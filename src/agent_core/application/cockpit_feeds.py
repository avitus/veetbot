"""Cockpit feeds: schema-checked snapshots with bounded history (Milestone 35).

docs/plan/cockpit.md, feeds. A feed's first publish registers its schema; a
later one is checked against it, or carries a new schema and so bumps the
schema version. Every snapshot fits its schema unless it reports a failed
attempt, history keeps at most `history_max` snapshots, and each publish is
one stream event. A schema never refers outside itself, so checking data
retrieves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    FEED_HISTORY_MAX,
    CockpitEventKind,
    Feed,
    FeedData,
    FeedSchemaDocument,
    FeedSnapshot,
)
from agent_core.domain.errors import CockpitValidationError, NotFoundError
from agent_core.ports.cockpit import JsonSchemaCheck
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory

# Reported times may run a little ahead of this clock; anything further is refused.
_CLOCK_SKEW = timedelta(seconds=60)


class CockpitFeedRefusedError(CockpitValidationError):
    """A publish the feed cannot take, named by a stable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FeedReading:
    """A feed with its latest snapshot and the history before it, oldest first."""

    feed: Feed
    current: FeedSnapshot | None
    history: tuple[FeedSnapshot, ...]


class CockpitFeeds:
    """Publish and read the principal's feeds."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        schema_check: JsonSchemaCheck,
        history_max: int,
    ) -> None:
        if not 1 <= history_max <= FEED_HISTORY_MAX:
            raise ValueError(f"history_max is between 1 and {FEED_HISTORY_MAX}")
        self._uow_factory = uow_factory
        self._clock = clock
        self._schema_check = schema_check
        self._history_max = history_max

    async def publish(
        self,
        principal: Principal,
        feed_id: str,
        *,
        data: FeedData,
        expected_interval_seconds: int,
        schema: FeedSchemaDocument | None = None,
        error: str | None = None,
        observed_at: datetime | None = None,
        preview: bool = False,
    ) -> tuple[Feed, FeedSnapshot]:
        """Store one snapshot, registering the feed or replacing its schema first."""

        require_scope(principal, "cockpit.write")
        now = self._clock.now()
        if observed_at is not None and observed_at > now + _CLOCK_SKEW:
            raise CockpitFeedRefusedError(
                "feed_observed_in_future", "the observation is in the future"
            )
        if schema is not None:
            problems = self._schema_check.schema_violations(schema)
            if problems:
                raise CockpitFeedRefusedError("feed_schema_invalid", f"the schema: {problems[0]}")
        async with self._uow_factory() as uow:
            feed = await uow.cockpit_feeds.get(principal, feed_id)
            target = (
                schema if schema is not None else None if feed is None else feed.schema_document
            )
            if target is None:
                raise CockpitFeedRefusedError(
                    "feed_schema_required", f"{feed_id} is new; its first publish carries a schema"
                )
            if error is None or data is not None:
                problems = self._schema_check.violations(target, data)
                if problems:
                    raise CockpitFeedRefusedError(
                        "feed_data_invalid", f"the data does not fit {feed_id}: {problems[0]}"
                    )
            try:
                if preview:
                    candidate = Feed(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        id=feed_id,
                        schema=target,
                        schema_version=(
                            1
                            if feed is None
                            else feed.schema_version + int(target != feed.schema_document)
                        ),
                        created_at=now if feed is None else feed.created_at,
                        updated_at=now,
                    )
                    return candidate, FeedSnapshot(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        feed_id=feed_id,
                        sequence=1,
                        observed_at=observed_at or now,
                        expected_interval_seconds=expected_interval_seconds,
                        error=error,
                        data=data,
                        schema_version=candidate.schema_version,
                        recorded_at=now,
                    )
                if feed is None:
                    feed, _created = await uow.cockpit_feeds.register(
                        Feed(
                            tenant_id=principal.tenant_id,
                            principal_id=principal.principal_id,
                            id=feed_id,
                            schema=target,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                elif schema is not None and schema != feed.schema_document:
                    feed = await uow.cockpit_feeds.replace_schema(
                        principal, feed_id, schema=schema, updated_at=now
                    )
                snapshot = FeedSnapshot(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    feed_id=feed_id,
                    sequence=1,
                    observed_at=observed_at or now,
                    expected_interval_seconds=expected_interval_seconds,
                    error=error,
                    data=data,
                    schema_version=feed.schema_version,
                    recorded_at=now,
                )
            except ValueError as exc:
                raise CockpitFeedRefusedError(
                    "feed_invalid", f"the snapshot is out of bounds: {exc}"
                ) from exc
            stored = await uow.cockpit_feeds.publish(snapshot, history_max=self._history_max)
            await uow.cockpit_events.append(
                principal, kind=CockpitEventKind.FEED_UPDATED, subject=feed_id, occurred_at=now
            )
        return feed, stored

    async def list_feeds(self, principal: Principal) -> list[Feed]:
        require_scope(principal, "cockpit.read")
        async with self._uow_factory() as uow:
            return sorted(await uow.cockpit_feeds.list_feeds(principal), key=lambda feed: feed.id)

    async def read(self, principal: Principal, feed_id: str, *, history: int) -> FeedReading:
        """The feed, its latest snapshot and up to `history` snapshots before it."""

        require_scope(principal, "cockpit.read")
        async with self._uow_factory() as uow:
            feed = await uow.cockpit_feeds.get(principal, feed_id)
            if feed is None:
                raise NotFoundError(f"feed {feed_id} not found")
            current = (await uow.cockpit_feeds.latest(principal, [feed_id])).get(feed_id)
            earlier = (
                await uow.cockpit_feeds.history(principal, feed_id, limit=history + 1)
                if history and current is not None
                else []
            )
        before = (
            tuple(item for item in earlier if item.sequence != current.sequence) if current else ()
        )
        return FeedReading(feed=feed, current=current, history=before[-history:] if history else ())
