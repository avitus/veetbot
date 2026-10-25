"""The platform feed sources and their projection on PostgreSQL (Milestone 35).

docs/plan/cockpit.md, platform feeds. Each source reads the PostgreSQL
repositories its routes read, and one projection pass publishes every enabled
feed and then none until something changes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck
from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.application.cockpit_platform_feeds import platform_feed_sources
from agent_core.application.cockpit_projector import PlatformFeedProjector
from agent_core.bootstrap import build
from tests.integration.m2_support import PRINCIPAL, database_settings

OWNER = PRINCIPAL.model_copy(update={"scopes": {"cockpit.read", "cockpit.write"}})
NOW = datetime(2026, 10, 24, 17, 0, tzinfo=UTC)


async def test_every_platform_source_projects_from_postgres(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "a", cockpit_enabled=True)
    sources = platform_feed_sources(
        email_account_ids=("personal",), schedules=True, calls=True, people=True
    )
    check = Draft202012SchemaCheck()
    async with build(settings=settings, storage="postgres", principal=OWNER) as composition:
        async with composition.uow_factory() as uow:
            for source in sources:
                data = await source.read(uow, OWNER, NOW)
                assert check.violations(source.schema, data) == [], source.feed_id
        projector = PlatformFeedProjector(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            feeds=CockpitFeeds(
                uow_factory=composition.uow_factory,
                clock=composition.clock,
                schema_check=check,
                history_max=288,
            ),
            principal=OWNER,
            sources=sources,
            refresh_seconds=300,
        )
        first = await projector.run_once()
        second = await projector.run_once()
        async with composition.uow_factory() as uow:
            feeds = await uow.cockpit_feeds.list_feeds(OWNER)
    assert (first, second) == (len(sources), 0)
    assert sorted(feed.id for feed in feeds) == sorted(source.feed_id for source in sources)
