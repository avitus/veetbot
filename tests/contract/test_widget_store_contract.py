"""Widget store contract, shared by the in-memory and PostgreSQL adapters."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.persistence.cockpit import InMemoryWidgetStore
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    BuildDiagnostic,
    DeviceClass,
    Layout,
    Placement,
    RevisionState,
    TileSize,
    WidgetSettings,
    WidgetState,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.cockpit import WidgetStore
from tests.contract.cockpit_support import (
    BUNDLE,
    BUNDLE_SHA256,
    COMMIT_A,
    COMMIT_B,
    Scenario,
    foreign,
    revision,
    scenarios,
    widget,
)
from tests.contract.support import NOW, principal

SCENARIOS, scenario = scenarios()
LATER = NOW + timedelta(minutes=5)


async def _live(
    store: WidgetStore, owner: Principal, slug: str = "weather", commit: str = COMMIT_A
) -> UUID:
    built = await store.add_revision(revision(owner, slug, state=RevisionState.PENDING_COMMIT))
    await store.set_revision_state(
        owner,
        built.id,
        expected=RevisionState.PENDING_COMMIT,
        state=RevisionState.COMMITTED,
        updated_at=LATER,
        commit_sha=commit,
    )
    await store.activate(owner, slug, built.id, updated_at=LATER)
    return built.id


@scenario
async def widgets_are_unique_per_owner_and_private(store: WidgetStore, owner: Principal) -> None:
    await store.create_widget(widget(owner, "weather"))
    await store.create_widget(widget(owner, "approvals-waiting"))
    with pytest.raises(ConflictError) as error:
        await store.create_widget(widget(owner, "weather"))
    assert error.value.reason == "widget_exists"
    assert [item.slug for item in await store.list_widgets(owner)] == [
        "approvals-waiting",
        "weather",
    ]
    assert (await store.get_widget(owner, "weather")).state is WidgetState.ACTIVE
    with pytest.raises(NotFoundError):
        await store.get_widget(foreign(owner), "weather")
    assert await store.list_widgets(foreign(owner)) == []
    await store.create_widget(widget(foreign(owner), "weather"))


@scenario
async def revisions_belong_to_a_widget_and_list_newest_first(
    store: WidgetStore, owner: Principal
) -> None:
    with pytest.raises(NotFoundError):
        await store.add_revision(revision(owner))
    await store.create_widget(widget(owner))
    first = await store.add_revision(revision(owner, at_minutes=1))
    second = await store.add_revision(revision(owner, at_minutes=2))
    assert await store.get_revision(owner, first.id) == first
    listed = await store.list_revisions(owner, "weather", limit=10)
    assert [item.id for item in listed] == [second.id, first.id]
    assert [item.id for item in await store.list_revisions(owner, "weather", limit=1)] == [
        second.id
    ]
    with pytest.raises(NotFoundError):
        await store.get_revision(foreign(owner), first.id)
    with pytest.raises(ConflictError) as error:
        await store.add_revision(first)
    assert error.value.reason == "revision_exists"


@scenario
async def revision_states_move_by_compare_and_set(store: WidgetStore, owner: Principal) -> None:
    await store.create_widget(widget(owner))
    pending = await store.add_revision(revision(owner))
    built = await store.set_revision_state(
        owner,
        pending.id,
        expected=RevisionState.VALIDATING,
        state=RevisionState.PENDING_COMMIT,
        updated_at=LATER,
        bundle=BUNDLE,
        bundle_sha256=BUNDLE_SHA256,
        kit_api=1,
    )
    assert built.state is RevisionState.PENDING_COMMIT
    assert built.bundle == BUNDLE
    assert built.updated_at == LATER
    with pytest.raises(ConflictError) as error:
        await store.set_revision_state(
            owner,
            pending.id,
            expected=RevisionState.VALIDATING,
            state=RevisionState.REJECTED,
            updated_at=LATER,
            diagnostics=(BuildDiagnostic(rule="lint.banned", file="widget.tsx", message="x"),),
        )
    assert error.value.reason == "revision_state_changed"
    committed = await store.set_revision_state(
        owner,
        pending.id,
        expected=RevisionState.PENDING_COMMIT,
        state=RevisionState.COMMITTED,
        updated_at=LATER,
        commit_sha=COMMIT_A,
    )
    assert committed.commit_sha == COMMIT_A
    assert committed.bundle == BUNDLE
    rejected = await store.add_revision(revision(owner))
    refused = await store.set_revision_state(
        owner,
        rejected.id,
        expected=RevisionState.VALIDATING,
        state=RevisionState.REJECTED,
        updated_at=LATER,
        diagnostics=(BuildDiagnostic(rule="lint.banned", file="widget.tsx", message="x"),),
    )
    assert [item.rule for item in refused.diagnostics] == ["lint.banned"]
    with pytest.raises(NotFoundError):
        await store.set_revision_state(
            foreign(owner),
            pending.id,
            expected=RevisionState.COMMITTED,
            state=RevisionState.LIVE,
            updated_at=LATER,
            commit_sha=COMMIT_A,
        )


@scenario
async def activation_supersedes_the_previous_live_revision(
    store: WidgetStore, owner: Principal
) -> None:
    await store.create_widget(widget(owner))
    unbuilt = await store.add_revision(revision(owner))
    with pytest.raises(ConflictError) as error:
        await store.activate(owner, "weather", unbuilt.id, updated_at=LATER)
    assert error.value.reason == "revision_not_committed"
    first = await _live(store, owner)
    second = await _live(store, owner, commit=COMMIT_B)
    assert (await store.get_widget(owner, "weather")).live_revision_id == second
    assert (await store.get_revision(owner, first)).state is RevisionState.SUPERSEDED
    assert (await store.get_revision(owner, second)).state is RevisionState.LIVE
    live = [
        item
        for item in await store.list_revisions(owner, "weather", limit=10)
        if item.state is RevisionState.LIVE
    ]
    assert [item.id for item in live] == [second]


@scenario
async def deletion_keeps_history_and_leaves_every_layout(
    store: WidgetStore, owner: Principal
) -> None:
    await store.create_widget(widget(owner))
    await store.create_widget(widget(owner, "markets"))
    live = await _live(store, owner)
    for device_class in (DeviceClass.WIDE, DeviceClass.COMPACT):
        await store.put_layout(
            Layout(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                device_class=device_class,
                placements=(
                    Placement(slug="weather", size=TileSize.SM),
                    Placement(slug="markets", size=TileSize.TALL),
                ),
                revision=1,
                updated_at=NOW,
            ),
            expected_revision=None,
        )
    deleted = await store.mark_deleted(owner, "weather", updated_at=LATER)
    assert deleted.state is WidgetState.DELETED
    assert deleted.live_revision_id == live
    assert [item.slug for item in await store.list_widgets(owner)] == ["markets"]
    assert {item.slug for item in await store.list_widgets(owner, include_deleted=True)} == {
        "markets",
        "weather",
    }
    for device_class in (DeviceClass.WIDE, DeviceClass.COMPACT):
        layout = await store.get_layout(owner, device_class)
        assert layout is not None
        assert [item.slug for item in layout.placements] == ["markets"]
        assert layout.revision == 2
    assert len(await store.list_revisions(owner, "weather", limit=10)) == 1
    restored = await store.activate(owner, "weather", live, updated_at=LATER)
    assert restored.state is WidgetState.ACTIVE


@scenario
async def bundles_are_served_by_digest_to_their_owner(store: WidgetStore, owner: Principal) -> None:
    assert await store.bundle(owner, BUNDLE_SHA256) is None
    await store.create_widget(widget(owner))
    await store.add_revision(revision(owner, state=RevisionState.PENDING_COMMIT))
    assert await store.bundle(owner, BUNDLE_SHA256) == BUNDLE
    assert await store.bundle(foreign(owner), BUNDLE_SHA256) is None


@scenario
async def settings_are_private_per_widget(store: WidgetStore, owner: Principal) -> None:
    await store.create_widget(widget(owner))
    assert await store.get_settings(owner, "weather") is None
    values = WidgetSettings(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        slug="weather",
        values={"place": "Example City"},
        updated_at=NOW,
    )
    assert await store.put_settings(values) == values
    replaced = values.model_copy(update={"values": {"place": "Elsewhere"}, "updated_at": LATER})
    await store.put_settings(replaced)
    assert await store.get_settings(owner, "weather") == replaced
    assert await store.get_settings(foreign(owner), "weather") is None
    with pytest.raises(NotFoundError):
        await store.put_settings(values.model_copy(update={"slug": "markets"}))


@scenario
async def layouts_change_by_compare_and_set(store: WidgetStore, owner: Principal) -> None:
    layout = Layout(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        device_class=DeviceClass.REGULAR,
        placements=(Placement(slug="weather", size=TileSize.SM),),
        revision=1,
        updated_at=NOW,
    )
    assert await store.get_layout(owner, DeviceClass.REGULAR) is None
    assert await store.put_layout(layout, expected_revision=None) == layout
    with pytest.raises(ConflictError) as error:
        await store.put_layout(layout, expected_revision=None)
    assert error.value.reason == "layout_changed"
    moved = layout.model_copy(
        update={"placements": (Placement(slug="weather", size=TileSize.LG),), "updated_at": LATER}
    )
    stored = await store.put_layout(moved, expected_revision=1)
    assert stored.revision == 2
    assert stored.placements[0].size is TileSize.LG
    with pytest.raises(ConflictError):
        await store.put_layout(moved, expected_revision=1)
    assert await store.get_layout(foreign(owner), DeviceClass.REGULAR) is None


@scenario
async def erasure_removes_only_the_owners_cockpit(store: WidgetStore, owner: Principal) -> None:
    await store.create_widget(widget(owner))
    await store.add_revision(revision(owner, state=RevisionState.PENDING_COMMIT))
    await store.create_widget(widget(foreign(owner)))
    assert await store.erase_principal(owner) >= 2
    assert await store.list_widgets(owner, include_deleted=True) == []
    assert await store.bundle(owner, BUNDLE_SHA256) is None
    assert [item.slug for item in await store.list_widgets(foreign(owner))] == ["weather"]


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda check: check.__name__)
async def test_in_memory_widget_store(check: Scenario) -> None:
    await check(InMemoryWidgetStore(), principal())
