"""Cockpit publication rules outside the hard gates (docs/plan/cockpit.md, writing)."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.application.cockpit import CockpitPublicationLimits
from agent_core.domain.cockpit import RevisionState
from agent_core.domain.errors import ConflictError, RateLimitedError
from agent_core.ports.cockpit import BuildScreen, WidgetBuild, WidgetBuildFailedError
from tests.gates.cockpit_publication_support import PRINCIPAL, Stack, package, publication_stack


async def _revisions(stack: Stack) -> int:
    async with stack.uow_factory() as uow:
        return len(await uow.cockpit_widgets.list_revisions(PRINCIPAL, "weather", limit=100))


async def test_an_update_must_name_the_live_revision_it_changes() -> None:
    async with publication_stack() as stack:
        first = await stack.publication.write(
            PRINCIPAL, "weather", package(), expected_revision=None
        )
        with pytest.raises(ConflictError) as created_twice:
            await stack.publication.write(PRINCIPAL, "weather", package(), expected_revision=None)
        assert created_twice.value.reason == "revision_changed"
        await stack.outbox.run_once()
        with pytest.raises(ConflictError):
            await stack.publication.write(
                PRINCIPAL, "weather", package("v2"), expected_revision=UUID(int=9)
            )
        updated = await stack.publication.write(
            PRINCIPAL, "weather", package("v2"), expected_revision=first.id
        )
        assert updated.state is RevisionState.PENDING_COMMIT


async def test_commits_are_rate_limited_per_widget_and_rejections_do_not_count() -> None:
    async with publication_stack() as stack:
        stack.publication._limits = CockpitPublicationLimits(commits_per_widget_per_hour=2)
        for marker in ("REJECT", "REJECT", "REJECT"):
            rejected = await stack.publication.write(
                PRINCIPAL, "weather", {**package(), "widget.tsx": marker}, expected_revision=None
            )
            assert rejected.state is RevisionState.REJECTED
        live = None
        for marker in ("v1", "v2"):
            revision = await stack.publication.write(
                PRINCIPAL, "weather", package(marker), expected_revision=live
            )
            await stack.outbox.run_once()
            live = revision.id
        with pytest.raises(RateLimitedError) as limited:
            await stack.publication.write(
                PRINCIPAL, "weather", package("v3"), expected_revision=live
            )
        assert limited.value.reason == "cockpit_widget_rate"
        stack.later(3601)
        await stack.publication.write(PRINCIPAL, "weather", package("v3"), expected_revision=live)


async def test_commits_are_rate_limited_per_principal() -> None:
    async with publication_stack() as stack:
        stack.publication._limits = CockpitPublicationLimits(commits_per_principal_per_hour=2)
        for slug in ("weather", "markets"):
            await stack.publication.write(PRINCIPAL, slug, package(), expected_revision=None)
        with pytest.raises(RateLimitedError) as limited:
            await stack.publication.write(PRINCIPAL, "flights", package(), expected_revision=None)
        assert limited.value.reason == "cockpit_principal_rate"


async def test_a_build_without_a_verdict_records_nothing() -> None:
    class Broken:
        async def build(self, *args: object, screen: BuildScreen, **kwargs: object) -> WidgetBuild:
            raise WidgetBuildFailedError("the builder exited with 2")

    async with publication_stack() as stack:
        stack.publication._builder = Broken()
        with pytest.raises(WidgetBuildFailedError):
            await stack.publication.write(PRINCIPAL, "weather", package(), expected_revision=None)
        async with stack.uow_factory() as uow:
            assert await uow.cockpit_widgets.list_widgets(PRINCIPAL, include_deleted=True) == []


async def test_restore_needs_a_deleted_widget_and_revert_a_live_one() -> None:
    async with publication_stack() as stack:
        first = await stack.publication.write(
            PRINCIPAL, "weather", package(), expected_revision=None
        )
        await stack.outbox.run_once()
        with pytest.raises(ConflictError, match="not deleted"):
            await stack.publication.restore(PRINCIPAL, "weather")
        await stack.publication.delete(PRINCIPAL, "weather")
        await stack.outbox.run_once()
        with pytest.raises(ConflictError, match="already deleted"):
            await stack.publication.delete(PRINCIPAL, "weather")
        with pytest.raises(ConflictError, match="restore it first"):
            await stack.publication.revert(PRINCIPAL, "weather", first.id)
        assert await _revisions(stack) == 1


async def test_one_entrys_unexpected_failure_does_not_block_other_widgets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with publication_stack() as stack:
        weather = await stack.publication.write(
            PRINCIPAL, "weather", package(), expected_revision=None
        )
        markets = await stack.publication.write(
            PRINCIPAL, "markets", package(), expected_revision=None
        )
        attempt = stack.outbox._attempt
        broken = {"weather"}

        async def attempt_or_break(entry: object) -> int:
            if getattr(entry, "slug", None) in broken:
                raise KeyError("tree")
            return await attempt(entry)  # type: ignore[arg-type]

        stack.outbox._attempt = attempt_or_break  # type: ignore[method-assign]
        with caplog.at_level("ERROR", logger="agent_core.application.cockpit"):
            assert await stack.outbox.run_once() == 1
        assert "cockpit_outbox_entry_failed" in caplog.text
        async with stack.uow_factory() as uow:
            live_markets = await uow.cockpit_widgets.get_revision(PRINCIPAL, markets.id)
            pending_weather = await uow.cockpit_widgets.get_revision(PRINCIPAL, weather.id)
            [entry] = await uow.cockpit_outbox.due(
                "tenant-a", now=stack.clock.now() + timedelta(days=1), limit=10
            )
        assert live_markets.state is RevisionState.LIVE
        assert pending_weather.state is RevisionState.PENDING_COMMIT
        assert entry.slug == "weather" and entry.attempts == 1
        assert entry.last_error is not None and "KeyError" in entry.last_error
        assert entry.next_attempt_at > stack.clock.now()

        broken.clear()
        stack.later(3600)
        assert await stack.outbox.run_once() == 1
        async with stack.uow_factory() as uow:
            live_weather = await uow.cockpit_widgets.get_revision(PRINCIPAL, weather.id)
        assert live_weather.state is RevisionState.LIVE
