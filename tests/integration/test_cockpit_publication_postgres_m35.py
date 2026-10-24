"""Cockpit publication on PostgreSQL against the in-memory GitHub remote (Milestone 35).

The gate cases run in memory; this one walks the same path through the
PostgreSQL stores and event log: commit after a lost response, a human edit
that wins and is imported, the importer's cursor, delete and restore.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.cockpit import RevisionSource, RevisionState, WidgetState
from tests.contract.support import NOW
from tests.gates.cockpit_publication_support import package, publication_stack
from tests.integration.m2_support import PRINCIPAL, database_settings

BRANCH_REF = "/repos/avitus/veetbot/git/refs/heads/cockpit/widgets"


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
