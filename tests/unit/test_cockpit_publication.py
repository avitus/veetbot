"""Cockpit publication rules outside the hard gates (docs/plan/cockpit.md, writing)."""

from __future__ import annotations

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
