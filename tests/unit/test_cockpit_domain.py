"""Cockpit value types (docs/plan/cockpit.md, the domain model)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.domain.cockpit import (
    BuildDiagnostic,
    CockpitEvent,
    CockpitEventKind,
    DeviceClass,
    Feed,
    FeedSnapshot,
    FeedTrust,
    Layout,
    Placement,
    RevisionSource,
    RevisionState,
    TileSize,
    Widget,
    WidgetRevision,
    WidgetSettings,
    WidgetState,
    commit_trailers,
    widget_commit_message,
)

NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
OWNER: dict[str, Any] = {"tenant_id": "tenant-a", "principal_id": "principal-a"}
REVISION = UUID("00000000-0000-0000-0000-00000000c0c1")
BUNDLE = "(()=>{globalThis.VeetbotRealm.register(()=>null)})();"
FILES = {
    "widget.json": "{}",
    "widget.tsx": "export default function W() { return null; }",
    "fixtures/typical.json": "{}",
    "fixtures/empty.json": "{}",
    "fixtures/stale.json": "{}",
}


def revision(**changes: object) -> WidgetRevision:
    values: dict[str, object] = {
        "id": REVISION,
        **OWNER,
        "slug": "weather",
        "source": RevisionSource.ASSISTANT,
        "state": RevisionState.PENDING_COMMIT,
        "files": FILES,
        "bundle": BUNDLE,
        "bundle_sha256": hashlib.sha256(BUNDLE.encode()).hexdigest(),
        "kit_api": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return WidgetRevision.model_validate(values)


def test_slugs_and_feed_ids_follow_the_manifest_grammar() -> None:
    Widget(**OWNER, slug="weather", state=WidgetState.ACTIVE, created_at=NOW, updated_at=NOW)
    for slug in ("Weather", "w", "-weather", "a" * 49, "weather/../x"):
        with pytest.raises(ValidationError):
            Widget(**OWNER, slug=slug, state=WidgetState.ACTIVE, created_at=NOW, updated_at=NOW)
    for feed_id in ("https://evil.example/feed", "weather", "agent.", "Agent.weather"):
        with pytest.raises(ValidationError):
            Feed(**OWNER, id=feed_id, schema={}, created_at=NOW, updated_at=NOW)


def test_feed_trust_follows_its_namespace() -> None:
    platform = Feed(
        **OWNER, id="veetbot.approvals.pending", schema={}, created_at=NOW, updated_at=NOW
    )
    agent = Feed(**OWNER, id="agent.weather", schema={}, created_at=NOW, updated_at=NOW)
    assert platform.trust is FeedTrust.PLATFORM
    assert agent.trust is FeedTrust.UNTRUSTED
    with pytest.raises(ValidationError):
        Feed(
            **OWNER,
            id="agent.weather",
            schema={},
            trust=FeedTrust.PLATFORM,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValidationError):
        Feed(
            **OWNER,
            id="agent.weather",
            schema={"description": "x" * 9000},
            created_at=NOW,
            updated_at=NOW,
        )


def test_feed_snapshots_are_bounded() -> None:
    snapshot = FeedSnapshot(
        **OWNER,
        feed_id="agent.weather",
        sequence=1,
        observed_at=NOW,
        expected_interval_seconds=900,
        data={"temperature": 64},
        recorded_at=NOW,
    )
    assert snapshot.schema_version == 1
    for changes in (
        {"data": {"blob": "x" * (64 * 1024)}},
        {"expected_interval_seconds": 0},
        {"error": "x" * 201},
        {"sequence": 0},
    ):
        with pytest.raises(ValidationError):
            FeedSnapshot.model_validate({**snapshot.model_dump(), **changes})


def test_a_revision_state_fixes_what_it_carries() -> None:
    assert revision().state is RevisionState.PENDING_COMMIT
    with pytest.raises(ValidationError, match="bundle"):
        revision(bundle_sha256="0" * 64)
    with pytest.raises(ValidationError, match="commit"):
        revision(state=RevisionState.LIVE)
    live = revision(state=RevisionState.LIVE, commit_sha="a" * 40)
    assert live.commit_sha == "a" * 40
    with pytest.raises(ValidationError, match="diagnostic"):
        revision(state=RevisionState.REJECTED, bundle=None, bundle_sha256=None)
    rejected = revision(
        state=RevisionState.REJECTED,
        bundle=None,
        bundle_sha256=None,
        kit_api=None,
        diagnostics=(BuildDiagnostic(rule="lint.banned", file="widget.tsx", message="x"),),
    )
    assert rejected.bundle is None
    validating = revision(
        state=RevisionState.VALIDATING, bundle=None, bundle_sha256=None, kit_api=None
    )
    assert validating.bundle_sha256 is None


def test_revision_files_are_a_package() -> None:
    for files in (
        {**FILES, "../escape.json": "{}"},
        {**FILES, "helper.ts": "export {}"},
        {**FILES, "widget.tsx": "x" * (256 * 1024 + 1)},
    ):
        with pytest.raises(ValidationError):
            revision(files=files)
    with pytest.raises(ValidationError):
        revision(state="deleted")


def test_layout_places_each_widget_once() -> None:
    layout = Layout(
        **OWNER,
        device_class=DeviceClass.WIDE,
        placements=(
            Placement(slug="weather", size=TileSize.SM),
            Placement(slug="markets", size=TileSize.TALL),
        ),
        revision=1,
        updated_at=NOW,
    )
    assert [placement.slug for placement in layout.placements] == ["weather", "markets"]
    with pytest.raises(ValidationError):
        Layout.model_validate(
            {**layout.model_dump(), "placements": [{"slug": "weather", "size": "sm"}] * 2}
        )
    with pytest.raises(ValidationError):
        Placement.model_validate({"slug": "weather", "size": "huge"})


def test_settings_hold_bounded_scalars() -> None:
    settings = WidgetSettings(
        **OWNER, slug="weather", values={"place": "Example City", "units": 1}, updated_at=NOW
    )
    assert settings.values["place"] == "Example City"
    invalid: list[dict[str, Any]] = [
        {"nested": {"a": 1}},
        {"long": "x" * 201},
        {f"key_{index}": index for index in range(17)},
        {"Bad-Key": 1},
    ]
    for values in invalid:
        with pytest.raises(ValidationError):
            WidgetSettings.model_validate(
                {**OWNER, "slug": "weather", "values": values, "updated_at": NOW}
            )


def test_events_carry_only_a_kind_and_a_subject() -> None:
    event = CockpitEvent(
        **OWNER,
        sequence=7,
        kind=CockpitEventKind.WIDGET_REVISED,
        subject="weather",
        occurred_at=NOW,
    )
    assert event.kind.value == "widget.revised"
    with pytest.raises(ValidationError):
        CockpitEvent.model_validate({**event.model_dump(), "subject": "x" * 65})


def test_commit_messages_are_a_fixed_template_with_trailers() -> None:
    message = widget_commit_message("update", "weather", REVISION)
    assert message == (
        "cockpit: update weather\n\n"
        "Cockpit-Widget: weather\n"
        "Veetbot-Revision: 00000000-0000-0000-0000-00000000c0c1\n"
    )
    assert commit_trailers(message) == {
        "Cockpit-Widget": "weather",
        "Veetbot-Revision": str(REVISION),
    }
    assert commit_trailers("fix: a human commit\n\nno trailers here\n") == {}
    with pytest.raises(ValueError):
        widget_commit_message("rename", "weather", REVISION)
    with pytest.raises(ValueError):
        widget_commit_message("update", "Weather Station", REVISION)
