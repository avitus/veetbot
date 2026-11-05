"""What the Cockpit routes return and accept (docs/plan/cockpit.md, the routes).

The snapshot is everything a board needs at once; the stream then names what
changed and the client refetches. Settings and layouts are the owner's private
state and reach only the owner's own clients.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent_core.domain.cockpit import (
    LAYOUT_MAX_PLACEMENTS,
    BuildDiagnostic,
    DeviceClass,
    FeedTrust,
    Placement,
    RevisionSource,
    RevisionState,
    WidgetState,
)
from agent_core.domain.schedules import ScheduleRefresh

type SettingValue = str | int | float | bool


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeviceFailureCode(StrEnum):
    RENDER_FAILED = "render_failed"
    BUNDLE_FAILED = "bundle_failed"
    QUARANTINED = "quarantined"


class CockpitDeviceFailure(_View):
    revision_id: UUID
    code: DeviceFailureCode


class CockpitRuntimeView(_View):
    """The host page and realm assets the board loads, by content hash."""

    kit_api: int
    entry: str
    assets: dict[str, str]


class CockpitWidgetView(_View):
    slug: str
    revision_id: UUID
    bundle_sha256: str
    kit_api: int
    manifest: dict[str, JsonValue]
    settings: dict[str, SettingValue]
    # When the live revision last changed; the watchdog quarantines the newest first.
    changed_at: datetime


class CockpitLayoutView(_View):
    revision: int
    placements: tuple[Placement, ...]


class CockpitFeedEntryView(_View):
    observed_at: datetime
    expected_interval_seconds: int
    error: str | None
    data: JsonValue
    schema_version: int


class CockpitFeedView(_View):
    """A declared feed's current snapshot and, oldest first, the history before it."""

    trust: FeedTrust
    current: CockpitFeedEntryView | None
    history: tuple[CockpitFeedEntryView, ...]


class CockpitSnapshotView(_View):
    """The whole board; `cursor` is where the stream resumes from.

    `server_time` is the server's clock as it took the snapshot, so a client
    renders freshness with server-corrected time.
    """

    cursor: int
    server_time: datetime
    runtime: CockpitRuntimeView | None
    widgets: tuple[CockpitWidgetView, ...]
    layouts: dict[DeviceClass, CockpitLayoutView]
    feeds: dict[str, CockpitFeedView]


class CockpitRevisionView(_View):
    id: UUID
    state: RevisionState
    source: RevisionSource
    commit_sha: str | None
    commit_url: str | None
    diagnostics: tuple[BuildDiagnostic, ...]
    created_at: datetime
    updated_at: datetime


class CockpitImportFailureView(_View):
    id: UUID
    commit_sha: str
    commit_url: str | None
    diagnostics: tuple[BuildDiagnostic, ...]
    seen: bool
    created_at: datetime


class CockpitWidgetDetailView(_View):
    slug: str
    state: WidgetState
    live_revision_id: UUID | None
    settings_schema: dict[str, JsonValue] | None
    settings: dict[str, SettingValue]
    revisions: tuple[CockpitRevisionView, ...]
    import_failures: tuple[CockpitImportFailureView, ...]


class CockpitSettingsWrite(_View):
    values: dict[str, SettingValue]


class CockpitSettingsView(_View):
    slug: str
    values: dict[str, SettingValue]
    updated_at: datetime


class CockpitLayoutWrite(_View):
    placements: tuple[Placement, ...] = Field(max_length=LAYOUT_MAX_PLACEMENTS)
    expected_revision: int | None = Field(ge=1)


class CockpitRevertRequest(_View):
    revision_id: UUID


class CockpitRefreshView(_View):
    """What a refresh did with each feed-keeping schedule (ADR-0254)."""

    schedules: tuple[ScheduleRefresh, ...]


class CockpitChangeView(_View):
    """An owner change the outbox will commit; `queued` for a removal."""

    slug: str
    operation: Literal["write", "delete"]
    state: RevisionState | Literal["queued"]
    revision_id: UUID | None = None
    diagnostics: tuple[BuildDiagnostic, ...] = ()
