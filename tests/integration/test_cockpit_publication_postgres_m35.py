"""Cockpit publication on PostgreSQL against the in-memory GitHub remote (Milestone 35).

The gate cases run in memory; this one walks the same path through the
PostgreSQL stores and event log: commit after a lost response, a human edit
that wins and is imported, the importer's cursor, delete and restore.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.cockpit import RevisionSource, RevisionState, WidgetState
from agent_core.domain.errors import ConflictError
from tests.contract.support import NOW
from tests.gates.cockpit_publication_support import package, publication_stack
from tests.integration.m2_support import PRINCIPAL, database_settings

BRANCH_REF = "/repos/avitus/veetbot/git/refs/heads/cockpit/widgets"


async def test_a_delete_and_a_write_wait_for_the_widget_lock_before_checking_the_head(
    tmp_path: Path,
) -> None:
    """The worker's activation holds the row, so a revision check cannot act on a stale head."""
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with (
        build(settings=settings, storage="postgres") as composition,
        publication_stack(
            uow_factory=composition.uow_factory, clock=FixedClock(NOW), principal=PRINCIPAL
        ) as stack,
    ):
        first = await stack.publication.write(
            PRINCIPAL, "locked", package(), expected_revision=None
        )
        assert await stack.outbox.run_once() == 1
        async with stack.uow_factory() as uow:
            await uow.cockpit_widgets.activate(PRINCIPAL, "locked", first.id, updated_at=NOW)
            await uow.cockpit_widgets.add_revision(
                first.model_copy(update={"id": uuid4(), "state": RevisionState.PENDING_COMMIT})
            )
            write = asyncio.create_task(
                stack.publication.write(
                    PRINCIPAL, "locked", package("v2"), expected_revision=first.id
                )
            )
            delete = asyncio.create_task(
                stack.publication.delete(PRINCIPAL, "locked", expected_head=first.id)
            )
            done, _pending = await asyncio.wait({write, delete}, timeout=1)
            assert not done
        results = await asyncio.gather(write, delete, return_exceptions=True)
        assert all(
            isinstance(result, ConflictError) and result.reason == "revision_changed"
            for result in results
        ), results


async def test_a_queued_removal_refuses_a_write_and_entries_keep_their_order_on_postgres(
    tmp_path: Path,
) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with (
        build(settings=settings, storage="postgres") as composition,
        publication_stack(
            uow_factory=composition.uow_factory, clock=FixedClock(NOW), principal=PRINCIPAL
        ) as stack,
    ):
        first = await stack.publication.write(
            PRINCIPAL, "ordered", package("v1"), expected_revision=None
        )
        assert await stack.outbox.run_once() == 1

        second = await stack.publication.write(
            PRINCIPAL, "ordered", package("v2"), expected_revision=first.id
        )
        stack.remote.fail_next("PATCH", BRANCH_REF, "unavailable")
        assert await stack.outbox.run_once() == 0
        stack.clock.advance(timedelta(seconds=1))
        third = await stack.publication.write(
            PRINCIPAL, "ordered", package("v3"), expected_revision=second.id
        )
        assert await stack.outbox.run_once() == 0
        stack.later()
        assert await stack.outbox.run_once() == 2
        async with stack.uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "ordered")
        assert widget.live_revision_id == third.id

        await stack.publication.delete(PRINCIPAL, "ordered", expected_head=third.id)
        stack.remote.fail_next("PATCH", BRANCH_REF, "unavailable")
        assert await stack.outbox.run_once() == 0
        with pytest.raises(ConflictError) as refused:
            await stack.publication.write(
                PRINCIPAL, "ordered", package("v4"), expected_revision=third.id
            )
        assert refused.value.reason == "removal_queued"
        stack.later()
        assert await stack.outbox.run_once() == 1
        async with stack.uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "ordered")
        assert (widget.state, widget.live_revision_id) == (WidgetState.DELETED, third.id)


async def test_publication_commits_imports_and_round_trips_on_postgres(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with (
        build(settings=settings, storage="postgres") as composition,
        publication_stack(
            uow_factory=composition.uow_factory, clock=FixedClock(NOW), principal=PRINCIPAL
        ) as stack,
    ):
        first = await stack.publication.write(
            PRINCIPAL, "weather", package(), expected_revision=None
        )
        stack.remote.fail_next("PATCH", BRANCH_REF, "lost")
        assert await stack.outbox.run_once() == 0
        stack.later()
        assert await stack.outbox.run_once() == 1
        assert stack.trailer_count(first.id) == 1
        assert await stack.importer.run_once() == 2
        assert await stack.importer.run_once() == 0

        human = stack.remote.commit_as_person(
            {"cockpit/widgets/weather/widget.tsx": "export default () => null; // person\n"},
            message="tune the tile\n",
        )
        mine = await stack.publication.write(
            PRINCIPAL, "weather", package("v2"), expected_revision=first.id
        )
        assert await stack.outbox.run_once() == 1
        assert await stack.importer.run_once() == 1
        async with stack.uow_factory() as uow:
            assert (await uow.cockpit_widgets.get_revision(PRINCIPAL, mine.id)).state is (
                RevisionState.CONFLICTED
            )
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "weather")
            assert widget.live_revision_id is not None
            imported = await uow.cockpit_widgets.get_revision(PRINCIPAL, widget.live_revision_id)
        assert (imported.source, imported.commit_sha) == (RevisionSource.OWNER_GIT, human)

        await stack.publication.delete(PRINCIPAL, "weather")
        assert await stack.outbox.run_once() == 1
        restored = await stack.publication.restore(PRINCIPAL, "weather")
        assert await stack.outbox.run_once() == 1
        async with stack.uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "weather")
            events = await uow.cockpit_events.after(PRINCIPAL, sequence=0, limit=50)
        assert (widget.state, widget.live_revision_id) == (WidgetState.ACTIVE, restored.id)
        assert [event.kind.value for event in events] == [
            "widget.revised",
            "widget.revised",
            "widget.deleted",
            "widget.revised",
        ]
