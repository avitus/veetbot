"""Cockpit publication gates against the in-memory GitHub remote (Milestone 35).

docs/plan/cockpit.md, Hard gates 2 (its server half) and 5 to 9. The
publication service, outbox worker and importer run over the real GitHub App
adapter; the builder is scripted (see `cockpit_publication_support.py`), and
the sandbox suite repeats the screen and rejection gates with the real one.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from agent_core.domain.cockpit import (
    CockpitEventKind,
    DeviceClass,
    Layout,
    Placement,
    RevisionSource,
    RevisionState,
    TileSize,
    WidgetState,
    commit_trailers,
)
from agent_core.ports.cockpit import BranchMovedError, WidgetPathRefusedError
from tests.contract.fake_github import NAME, OWNER
from tests.gates.cockpit_publication_support import PRINCIPAL, Stack, package, publication_stack

BRANCH_REF = f"/repos/{OWNER}/{NAME}/git/refs/heads/cockpit/widgets"


async def _events(stack: Stack) -> list[tuple[CockpitEventKind, str]]:
    async with stack.uow_factory() as uow:
        events = await uow.cockpit_events.after(PRINCIPAL, sequence=0, limit=100)
    return [(event.kind, event.subject) for event in events]


async def _live(stack: Stack, slug: str = "weather") -> UUID | None:
    async with stack.uow_factory() as uow:
        return (await uow.cockpit_widgets.get_widget(PRINCIPAL, slug)).live_revision_id


async def _state(stack: Stack, revision_id: UUID) -> RevisionState:
    async with stack.uow_factory() as uow:
        return (await uow.cockpit_widgets.get_revision(PRINCIPAL, revision_id)).state


async def _create_live(stack: Stack, marker: str = "v1") -> UUID:
    revision = await stack.publication.write(
        PRINCIPAL, "weather", package(marker), expected_revision=await _current(stack)
    )
    await stack.outbox.run_once()
    assert await _state(stack, revision.id) is RevisionState.LIVE
    return revision.id


async def _current(stack: Stack) -> UUID | None:
    async with stack.uow_factory() as uow:
        try:
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "weather")
        except Exception:
            return None
    return widget.live_revision_id if widget.state is WidgetState.ACTIVE else None


async def test_a_rejected_package_leaves_no_outbox_entry_commit_or_event() -> None:
    async with publication_stack() as stack:
        head = await stack.repository.head()
        revision = await stack.publication.write(
            PRINCIPAL, "weather", {**package(), "widget.tsx": "REJECT"}, expected_revision=None
        )
        assert revision.state is RevisionState.REJECTED
        assert [diagnostic.rule for diagnostic in revision.diagnostics] == ["lint.banned"]
        assert (revision.bundle, revision.commit_sha) == (None, None)
        assert await stack.outbox.run_once() == 0
        assert await stack.repository.head() == head
        assert await _events(stack) == []
        async with stack.uow_factory() as uow:
            assert await uow.cockpit_outbox.due("tenant-a", now=stack.clock.now(), limit=10) == []


async def test_a_widget_goes_live_only_after_its_commit_and_an_outage_keeps_the_last_live() -> None:
    async with publication_stack() as stack:
        first = await stack.publication.write(
            PRINCIPAL, "weather", package("v1"), expected_revision=None
        )
        assert first.state is RevisionState.PENDING_COMMIT
        assert await _live(stack) is None
        assert await _events(stack) == []
        assert await stack.outbox.run_once() == 1
        async with stack.uow_factory() as uow:
            live = await uow.cockpit_widgets.get_revision(PRINCIPAL, first.id)
        assert live.state is RevisionState.LIVE
        assert live.commit_sha is not None
        assert live.commit_sha == await stack.repository.head()
        message = stack.remote.commits[live.commit_sha].message
        assert commit_trailers(message) == {
            "Cockpit-Widget": "weather",
            "Veetbot-Revision": str(first.id),
        }
        assert stack.head_files("weather") == package("v1")
        assert await _events(stack) == [(CockpitEventKind.WIDGET_REVISED, "weather")]

        second = await stack.publication.write(
            PRINCIPAL, "weather", package("v2"), expected_revision=first.id
        )
        stack.remote.outage = True
        for _ in range(3):
            assert await stack.outbox.run_once() == 0
            stack.later(1000)
        assert await _state(stack, second.id) is RevisionState.PENDING_COMMIT
        assert await _live(stack) == first.id
        assert stack.head_files("weather") == package("v1")
        async with stack.uow_factory() as uow:
            [entry] = await uow.cockpit_outbox.due("tenant-a", now=stack.clock.now(), limit=10)
        assert entry.attempts == 3 and entry.last_error

        stack.remote.outage = False
        assert await stack.outbox.run_once() == 1
        assert await _live(stack) == second.id
        assert await _state(stack, first.id) is RevisionState.SUPERSEDED
        assert stack.head_files("weather") == package("v2")


async def _a_lost_ref_update_reconciles_to_one_commit() -> None:
    async with publication_stack() as stack:
        revision = await stack.publication.write(
            PRINCIPAL, "weather", package(), expected_revision=None
        )
        stack.remote.fail_next("PATCH", BRANCH_REF, "lost")
        assert await stack.outbox.run_once() == 0
        assert await _state(stack, revision.id) is RevisionState.PENDING_COMMIT
        assert stack.trailer_count(revision.id) == 1
        stack.later()
        assert await stack.outbox.run_once() == 1
        assert await _state(stack, revision.id) is RevisionState.LIVE
        assert stack.trailer_count(revision.id) == 1


async def _a_concurrent_human_change_to_the_widget_wins_and_is_imported() -> None:
    async with publication_stack() as stack:
        first = await _create_live(stack)
        await stack.importer.run_once()
        human = stack.remote.commit_as_person(
            {"cockpit/widgets/weather/widget.tsx": "export default () => null; // person\n"},
            message="tune the weather tile\n",
        )
        mine = await stack.publication.write(
            PRINCIPAL, "weather", package("v2"), expected_revision=first
        )
        assert await stack.outbox.run_once() == 1
        assert await _state(stack, mine.id) is RevisionState.CONFLICTED
        assert await stack.repository.head() == human
        assert stack.trailer_count(mine.id) == 0

        assert await stack.importer.run_once() == 1
        async with stack.uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "weather")
            assert widget.live_revision_id is not None
            imported = await uow.cockpit_widgets.get_revision(PRINCIPAL, widget.live_revision_id)
        assert imported.source is RevisionSource.OWNER_GIT
        assert imported.commit_sha == human
        assert imported.files["widget.tsx"].endswith("// person\n")


async def test_an_ambiguous_commit_reconciles_once_and_a_person_wins() -> None:
    await _a_lost_ref_update_reconciles_to_one_commit()
    await _a_concurrent_human_change_to_the_widget_wins_and_is_imported()
    await _a_person_creating_the_same_widget_wins_on_a_long_branch()
    await _a_person_version_imported_while_a_create_waits_still_wins()


async def test_a_human_change_to_another_widget_does_not_conflict() -> None:
    async with publication_stack() as stack:
        first = await _create_live(stack)
        stack.remote.commit_as_person(
            {"cockpit/widgets/markets/widget.json": "{}"}, message="start markets\n"
        )
        mine = await stack.publication.write(
            PRINCIPAL, "weather", package("v2"), expected_revision=first
        )
        assert await stack.outbox.run_once() == 1
        assert await _state(stack, mine.id) is RevisionState.LIVE


async def test_every_publication_write_stays_in_one_widget_on_the_branch_and_never_forces() -> None:
    async with publication_stack() as stack:
        first = await _create_live(stack)
        await stack.publication.write(PRINCIPAL, "weather", package("v2"), expected_revision=first)
        await stack.outbox.run_once()
        await stack.publication.delete(PRINCIPAL, "weather")
        await stack.outbox.run_once()
        writes = [r for r in stack.remote.requests if r.method in {"POST", "PATCH"}]
        updates = [r for r in writes if r.method == "PATCH"]
        assert len(updates) == 3
        assert {r.path for r in updates} == {BRANCH_REF}
        assert all(r.body["force"] is False for r in updates)
        paths = {
            entry["path"]
            for r in writes
            if r.path.endswith("/git/trees")
            for entry in r.body["tree"]
        }
        assert paths - {"cockpit/widgets/README.md"} <= {
            f"cockpit/widgets/weather/{name}" for name in package()
        }
        assert not [r for r in writes if r.path.endswith("/git/refs")][1:]

        head = await stack.repository.head()
        assert head is not None
        before = len(stack.remote.requests)
        with pytest.raises(WidgetPathRefusedError):
            await stack.repository.write_widget(
                "weather", {"../../.github/workflows/x.yml": "x"}, parent_sha=head, message="x"
            )
        assert stack.remote.requests[before:] == []
        stale = stack.remote.commits[head].parents[0]
        with pytest.raises(BranchMovedError):
            await stack.repository.write_widget("weather", package(), parent_sha=stale, message="x")
        assert await stack.repository.head() == head


async def test_imports_activate_valid_human_commits_and_keep_the_live_widget_otherwise() -> None:
    async with publication_stack() as stack:
        live = await _create_live(stack)
        assert await stack.importer.run_once() == 2  # The branch root and our own commit.
        async with stack.uow_factory() as uow:
            assert [
                revision.source
                for revision in await uow.cockpit_widgets.list_revisions(
                    PRINCIPAL, "weather", limit=9
                )
            ] == [RevisionSource.ASSISTANT]

        created = stack.remote.commit_as_person(
            {f"cockpit/widgets/markets/{name}": text for name, text in package("m").items()},
            message="add markets\n",
        )
        broken = stack.remote.commit_as_person(
            {"cockpit/widgets/weather/widget.tsx": "REJECT"}, message="break weather\n"
        )
        sneaky = stack.remote.commit_as_person(
            {"src/agent_core/sneaky.py": "x"}, message="reach outside\n"
        )
        assert await stack.importer.run_once() == 3
        assert await stack.importer.run_once() == 0

        assert await _live(stack) == live
        async with stack.uow_factory() as uow:
            markets = await uow.cockpit_widgets.get_widget(PRINCIPAL, "markets")
            assert markets.live_revision_id is not None
            imported = await uow.cockpit_widgets.get_revision(PRINCIPAL, markets.live_revision_id)
            failures = await uow.cockpit_outbox.import_failures(PRINCIPAL)
        assert (imported.source, imported.state, imported.commit_sha) == (
            RevisionSource.OWNER_GIT,
            RevisionState.LIVE,
            created,
        )
        assert sorted((f.commit_sha, f.slug, f.diagnostics[0].rule) for f in failures) == sorted(
            [(broken, "weather", "lint.banned"), (sneaky, None, "import.out_of_scope")]
        )
        assert (CockpitEventKind.IMPORT_FAILED, "weather") in await _events(stack)
        assert (CockpitEventKind.WIDGET_REVISED, "markets") in await _events(stack)


async def test_a_forged_trailer_does_not_hide_a_human_commit() -> None:
    async with publication_stack() as stack:
        await stack.importer.run_once()
        stack.remote.commit_as_person(
            {f"cockpit/widgets/markets/{name}": text for name, text in package("m").items()},
            message="add markets\n\nVeetbot-Revision: 00000000-0000-0000-0000-000000000bad\n",
        )
        assert await stack.importer.run_once() == 1
        assert await _live(stack, "markets") is not None


async def test_delete_restore_and_revert_round_trip_through_the_branch() -> None:
    async with publication_stack() as stack:
        first = await _create_live(stack, "v1")
        second = await stack.publication.write(
            PRINCIPAL, "weather", package("v2"), expected_revision=first
        )
        await stack.outbox.run_once()
        async with stack.uow_factory() as uow:
            await uow.cockpit_widgets.put_layout(
                Layout(
                    tenant_id=PRINCIPAL.tenant_id,
                    principal_id=PRINCIPAL.principal_id,
                    device_class=DeviceClass.WIDE,
                    placements=(Placement(slug="weather", size=TileSize.MD),),
                    revision=1,
                    updated_at=stack.clock.now(),
                ),
                expected_revision=None,
            )

        await stack.publication.delete(PRINCIPAL, "weather")
        async with stack.uow_factory() as uow:
            assert (await uow.cockpit_widgets.get_widget(PRINCIPAL, "weather")).state is (
                WidgetState.ACTIVE
            )
        assert await stack.outbox.run_once() == 1
        assert stack.head_files("weather") == {}
        async with stack.uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "weather")
            layout = await uow.cockpit_widgets.get_layout(PRINCIPAL, DeviceClass.WIDE)
        assert widget.state is WidgetState.DELETED
        assert layout is not None and layout.placements == ()
        assert (CockpitEventKind.WIDGET_DELETED, "weather") in await _events(stack)

        restored = await stack.publication.restore(PRINCIPAL, "weather")
        assert restored.source is RevisionSource.REVERT
        assert await stack.outbox.run_once() == 1
        assert stack.head_files("weather") == package("v2")
        head = await stack.repository.head()
        assert head is not None
        assert stack.remote.commits[head].message.startswith("cockpit: restore weather\n")
        async with stack.uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "weather")
        assert (widget.state, widget.live_revision_id) == (WidgetState.ACTIVE, restored.id)

        screens_before = len(stack.builder.screens)
        reverted = await stack.publication.revert(PRINCIPAL, "weather", first)
        assert len(stack.builder.screens) == screens_before + 1
        assert reverted.id not in {first, second.id, restored.id}
        assert await stack.outbox.run_once() == 1
        assert await _live(stack) == reverted.id
        assert stack.head_files("weather") == package("v1")
        head = await stack.repository.head()
        assert head is not None
        assert stack.remote.commits[head].message.startswith("cockpit: revert weather\n")


async def test_the_screen_sees_every_name_and_setting_and_the_exact_commit_message() -> None:
    from agent_core.domain.cockpit import WidgetSettings
    from agent_core.domain.people import Person, PersonIdentifier

    async with publication_stack() as stack:
        first = await _create_live(stack)
        now = stack.clock.now()
        async with stack.uow_factory() as uow:
            await uow.people.put(
                Person(
                    id=UUID(int=501),
                    tenant_id=PRINCIPAL.tenant_id,
                    principal_id=PRINCIPAL.principal_id,
                    display_name="Jordan Example",
                    created_at=now,
                    updated_at=now,
                ),
                expected_revision=0,
            )
            await uow.people.put(
                PersonIdentifier(
                    id=UUID(int=502),
                    tenant_id=PRINCIPAL.tenant_id,
                    principal_id=PRINCIPAL.principal_id,
                    person_id=UUID(int=501),
                    identifier_kind="name",
                    namespace="owner",
                    value="Jojo",
                    verification="owner_confirmed",
                    valid_from=now,
                    created_at=now,
                    updated_at=now,
                ),
                expected_revision=0,
            )
            await uow.cockpit_widgets.put_settings(
                WidgetSettings(
                    tenant_id=PRINCIPAL.tenant_id,
                    principal_id=PRINCIPAL.principal_id,
                    slug="weather",
                    values={"place": "Example City", "units": 2},
                    updated_at=now,
                )
            )
        update = await stack.publication.write(
            PRINCIPAL, "weather", package("v2"), expected_revision=first
        )
        screen = stack.builder.screens[-1]
        assert {"Avery Quinn", "Jordan Example", "Jojo"} <= set(screen.names)
        assert screen.settings_values == ("Example City",)
        await stack.outbox.run_once()
        async with stack.uow_factory() as uow:
            committed = await uow.cockpit_widgets.get_revision(PRINCIPAL, update.id)
        assert committed.commit_sha is not None
        assert screen.commit_message == stack.remote.commits[committed.commit_sha].message


async def _a_person_creating_the_same_widget_wins_on_a_long_branch() -> None:
    from agent_core.application.cockpit import CockpitPublicationLimits

    async with publication_stack() as stack:
        stack.outbox._limits = CockpitPublicationLimits(history_depth=3)
        for index in range(5):
            stack.remote.commit_as_person(
                {f"cockpit/widgets/other-{index}/widget.json": "{}"}, message="other\n"
            )
        await stack.importer.run_once()
        human = stack.remote.commit_as_person(
            {f"cockpit/widgets/weather/{name}": text for name, text in package("p").items()},
            message="a person adds weather\n",
        )
        mine = await stack.publication.write(
            PRINCIPAL, "weather", package(), expected_revision=None
        )
        assert await stack.outbox.run_once() == 1
        assert await _state(stack, mine.id) is RevisionState.CONFLICTED
        assert await stack.repository.head() == human


async def _a_person_version_imported_while_a_create_waits_still_wins() -> None:
    async with publication_stack() as stack:
        await stack.importer.run_once()
        mine = await stack.publication.write(
            PRINCIPAL, "weather", package(), expected_revision=None
        )
        human = stack.remote.commit_as_person(
            {f"cockpit/widgets/weather/{name}": text for name, text in package("p").items()},
            message="a person adds weather\n",
        )
        assert await stack.importer.run_once() == 1
        assert await stack.outbox.run_once() == 1
        assert await _state(stack, mine.id) is RevisionState.CONFLICTED
        assert await stack.repository.head() == human
        async with stack.uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, "weather")
            assert widget.live_revision_id is not None
            live = await uow.cockpit_widgets.get_revision(PRINCIPAL, widget.live_revision_id)
        assert live.source is RevisionSource.OWNER_GIT
