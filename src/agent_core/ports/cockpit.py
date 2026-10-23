"""Cockpit widget, feed, event and repository ports (Milestone 35)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    BuildDiagnostic,
    CockpitEvent,
    CockpitEventKind,
    CommitOutboxEntry,
    DeviceClass,
    Feed,
    FeedSchemaDocument,
    FeedSnapshot,
    ImportFailure,
    Layout,
    RevisionState,
    Widget,
    WidgetRevision,
    WidgetSettings,
)


class WidgetStore(Protocol):
    """Widgets, their revisions and bundles, private settings, and layouts.

    A slug is unique per principal (`ConflictError` reason `widget_exists`).
    A revision's state moves only by compare-and-set: `set_revision_state`
    raises `ConflictError` reason `revision_state_changed` when the stored
    state is not the expected one. `activate` makes a committed revision live
    and supersedes the previous live revision in the same call. A layout is
    written by compare-and-set on its revision (`layout_changed`). Reads by
    another principal raise `NotFoundError`, indistinguishable from absence.
    """

    async def create_widget(self, widget: Widget) -> Widget: ...

    async def get_widget(self, principal: Principal, slug: str) -> Widget: ...

    async def list_widgets(
        self, principal: Principal, *, include_deleted: bool = False
    ) -> list[Widget]: ...

    async def add_revision(self, revision: WidgetRevision) -> WidgetRevision: ...

    async def get_revision(self, principal: Principal, revision_id: UUID) -> WidgetRevision: ...

    async def list_revisions(
        self, principal: Principal, slug: str, *, limit: int
    ) -> list[WidgetRevision]:
        """Newest first."""
        ...

    async def set_revision_state(
        self,
        principal: Principal,
        revision_id: UUID,
        *,
        expected: RevisionState,
        state: RevisionState,
        updated_at: datetime,
        bundle: str | None = None,
        bundle_sha256: str | None = None,
        kit_api: int | None = None,
        commit_sha: str | None = None,
        diagnostics: Sequence[BuildDiagnostic] = (),
    ) -> WidgetRevision: ...

    async def activate(
        self, principal: Principal, slug: str, revision_id: UUID, *, updated_at: datetime
    ) -> Widget: ...

    async def mark_deleted(
        self, principal: Principal, slug: str, *, updated_at: datetime
    ) -> Widget:
        """Mark the widget deleted and remove it from every layout; revisions stay."""
        ...

    async def bundle(self, principal: Principal, bundle_sha256: str) -> str | None:
        """A built bundle by digest, while any of the principal's revisions holds it."""
        ...

    async def get_settings(self, principal: Principal, slug: str) -> WidgetSettings | None: ...

    async def put_settings(self, settings: WidgetSettings) -> WidgetSettings: ...

    async def get_layout(
        self, principal: Principal, device_class: DeviceClass
    ) -> Layout | None: ...

    async def put_layout(self, layout: Layout, *, expected_revision: int | None) -> Layout:
        """Create (expected None) or replace the layout whose revision is expected."""
        ...

    async def erase_principal(self, principal: Principal) -> int: ...


class WidgetCommitOutbox(Protocol):
    """Accepted revisions waiting for their commit, and human commits that failed import.

    One entry per revision (`ConflictError` reason `commit_queued`). `due`
    returns the tenant's entries whose next attempt is at or before `now`,
    oldest first.
    """

    async def enqueue(self, entry: CommitOutboxEntry) -> CommitOutboxEntry: ...

    async def due(
        self, tenant_id: str, *, now: datetime, limit: int
    ) -> list[CommitOutboxEntry]: ...

    async def reschedule(
        self,
        principal: Principal,
        revision_id: UUID,
        *,
        attempts: int,
        next_attempt_at: datetime,
        last_error: str | None,
    ) -> CommitOutboxEntry: ...

    async def complete(self, principal: Principal, revision_id: UUID) -> None: ...

    async def record_import_failure(self, failure: ImportFailure) -> ImportFailure: ...

    async def import_failures(
        self, principal: Principal, *, unseen_only: bool = False
    ) -> list[ImportFailure]:
        """Newest first."""
        ...

    async def mark_seen(self, principal: Principal, failure_ids: Sequence[UUID]) -> int: ...

    async def erase_principal(self, principal: Principal) -> int: ...


class FeedStore(Protocol):
    """Feeds, their current snapshot and bounded history.

    `register` creates a feed or returns the existing one unchanged;
    `replace_schema` bumps the schema version. `publish` assigns the next
    sequence for the feed, refuses a snapshot whose schema version is not the
    feed's (`ConflictError` reason `feed_schema_changed`), and keeps at most
    `history_max` snapshots.
    """

    async def register(self, feed: Feed) -> tuple[Feed, bool]: ...

    async def get(self, principal: Principal, feed_id: str) -> Feed | None: ...

    async def list_feeds(self, principal: Principal) -> list[Feed]: ...

    async def replace_schema(
        self,
        principal: Principal,
        feed_id: str,
        *,
        schema: FeedSchemaDocument,
        updated_at: datetime,
    ) -> Feed: ...

    async def publish(self, snapshot: FeedSnapshot, *, history_max: int) -> FeedSnapshot:
        """The snapshot's own sequence is ignored; the stored one is returned."""
        ...

    async def latest(
        self, principal: Principal, feed_ids: Sequence[str]
    ) -> dict[str, FeedSnapshot]: ...

    async def history(
        self, principal: Principal, feed_id: str, *, limit: int
    ) -> list[FeedSnapshot]:
        """Oldest first, the newest `limit` snapshots."""
        ...

    async def prune_recorded_before(self, principal: Principal, *, before: datetime) -> int:
        """Drop history older than `before`, always keeping each feed's latest snapshot."""
        ...

    async def erase_principal(self, principal: Principal) -> int: ...


class CockpitEventLog(Protocol):
    """The durable, sequenced events the Cockpit stream replays.

    Sequences increase strictly in append order; gaps are allowed.
    """

    async def append(
        self,
        principal: Principal,
        *,
        kind: CockpitEventKind,
        subject: str,
        occurred_at: datetime,
    ) -> CockpitEvent: ...

    async def after(
        self, principal: Principal, *, sequence: int, limit: int
    ) -> list[CockpitEvent]: ...

    async def oldest_sequence(self, principal: Principal) -> int | None: ...

    async def latest_sequence(self, principal: Principal) -> int | None: ...

    async def prune_before(self, tenant_id: str, *, before: datetime) -> int: ...

    async def erase_principal(self, principal: Principal) -> int: ...


class BranchMovedError(Exception):
    """The branch head is not the parent the write expected."""

    def __init__(self, head: str | None) -> None:
        super().__init__(f"the widget branch moved to {head}")
        self.head = head


class WidgetPathRefusedError(Exception):
    """A write named a path outside one widget's directory."""


@dataclass(frozen=True, slots=True)
class WidgetCommit:
    sha: str
    parent_sha: str | None
    message: str
    trailers: Mapping[str, str]
    changed_slugs: tuple[str, ...]
    outside_widgets: bool


class WidgetRepository(Protocol):
    """The public widget branch: only `cockpit/widgets/<slug>/`, fast-forward only.

    Writes are compare-and-set on the head (`BranchMovedError`) and refuse any
    path outside the one widget's directory (`WidgetPathRefusedError`) before
    anything reaches the remote.
    """

    async def head(self) -> str | None: ...

    async def ensure_branch(self) -> str: ...

    async def write_widget(
        self,
        slug: str,
        files: Mapping[str, str],
        *,
        parent_sha: str,
        message: str,
    ) -> str: ...

    async def remove_widget(self, slug: str, *, parent_sha: str, message: str) -> str: ...

    async def read_widget(self, slug: str, *, at: str) -> dict[str, str] | None: ...

    async def commits_since(self, sha: str | None, *, limit: int) -> list[WidgetCommit]:
        """Oldest first, the commits after `sha` (from the root when None)."""
        ...

    async def find_revision(self, revision_id: UUID, *, limit: int) -> str | None:
        """The newest of the last `limit` commits whose trailer names the revision."""
        ...
