"""Cockpit widget, outbox, feed and event stores (Milestone 35).

The in-memory adapters back the contract suites and in-memory composition;
the PostgreSQL adapters run under the unit of work's transaction. Both pass
one contract suite (tests/contract/test_*_contract.py).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agent_core.adapters.persistence.sqlalchemy_models import (
    CockpitCommitOutboxRow,
    CockpitEventRow,
    CockpitFeedRow,
    CockpitFeedSnapshotRow,
    CockpitImportFailureRow,
    CockpitLayoutRow,
    CockpitWidgetRevisionRow,
    CockpitWidgetRow,
    CockpitWidgetSettingsRow,
)
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
    FeedTrust,
    ImportFailure,
    Layout,
    OutboxOperation,
    Placement,
    RevisionSource,
    RevisionState,
    Widget,
    WidgetRevision,
    WidgetSettings,
    WidgetState,
    cockpit_channel,
)
from agent_core.domain.errors import ConflictError, NotFoundError

# A revision with a commit may be made live: a fresh commit, or a restore or revert of one.
ACTIVATABLE = frozenset({RevisionState.COMMITTED, RevisionState.LIVE, RevisionState.SUPERSEDED})

type Owner = tuple[str, str]


def _owner(principal: Principal) -> Owner:
    return principal.tenant_id, principal.principal_id


def _revised(
    stored: WidgetRevision,
    *,
    state: RevisionState,
    updated_at: datetime,
    bundle: str | None,
    bundle_sha256: str | None,
    kit_api: int | None,
    commit_sha: str | None,
    diagnostics: Sequence[BuildDiagnostic],
) -> WidgetRevision:
    values: dict[str, Any] = {**stored.model_dump(), "state": state, "updated_at": updated_at}
    for name, value in (
        ("bundle", bundle),
        ("bundle_sha256", bundle_sha256),
        ("kit_api", kit_api),
        ("commit_sha", commit_sha),
    ):
        if value is not None:
            values[name] = value
    if diagnostics:
        values["diagnostics"] = tuple(diagnostics)
    return WidgetRevision.model_validate(values)


def _without(layout: Layout, slug: str, updated_at: datetime) -> Layout | None:
    placements = tuple(item for item in layout.placements if item.slug != slug)
    if placements == layout.placements:
        return None
    return layout.model_copy(
        update={"placements": placements, "revision": layout.revision + 1, "updated_at": updated_at}
    )


class InMemoryWidgetStore:
    def __init__(self) -> None:
        self._widgets: dict[Owner, dict[str, Widget]] = {}
        self._revisions: dict[Owner, dict[UUID, WidgetRevision]] = {}
        self._settings: dict[Owner, dict[str, WidgetSettings]] = {}
        self._layouts: dict[Owner, dict[DeviceClass, Layout]] = {}
        self._lock = asyncio.Lock()

    def _widget(self, principal: Principal, slug: str) -> Widget:
        found = self._widgets.get(_owner(principal), {}).get(slug)
        if found is None:
            raise NotFoundError(f"widget {slug} not found")
        return found

    def _revision(self, principal: Principal, revision_id: UUID) -> WidgetRevision:
        found = self._revisions.get(_owner(principal), {}).get(revision_id)
        if found is None:
            raise NotFoundError(f"widget revision {revision_id} not found")
        return found

    async def create_widget(self, widget: Widget) -> Widget:
        async with self._lock:
            owned = self._widgets.setdefault((widget.tenant_id, widget.principal_id), {})
            if widget.slug in owned:
                raise ConflictError(f"widget {widget.slug} exists", reason="widget_exists")
            owned[widget.slug] = widget
            return widget

    async def get_widget(self, principal: Principal, slug: str) -> Widget:
        async with self._lock:
            return self._widget(principal, slug)

    async def list_widgets(
        self, principal: Principal, *, include_deleted: bool = False
    ) -> list[Widget]:
        async with self._lock:
            widgets = sorted(
                self._widgets.get(_owner(principal), {}).values(), key=lambda item: item.slug
            )
            return [item for item in widgets if include_deleted or item.state is WidgetState.ACTIVE]

    async def add_revision(self, revision: WidgetRevision) -> WidgetRevision:
        owner = Principal(tenant_id=revision.tenant_id, principal_id=revision.principal_id)
        async with self._lock:
            self._widget(owner, revision.slug)
            owned = self._revisions.setdefault(_owner(owner), {})
            if revision.id in owned:
                raise ConflictError(f"revision {revision.id} exists", reason="revision_exists")
            owned[revision.id] = revision
            return revision

    async def get_revision(self, principal: Principal, revision_id: UUID) -> WidgetRevision:
        async with self._lock:
            return self._revision(principal, revision_id)

    async def list_revisions(
        self, principal: Principal, slug: str, *, limit: int
    ) -> list[WidgetRevision]:
        async with self._lock:
            revisions = [
                item
                for item in self._revisions.get(_owner(principal), {}).values()
                if item.slug == slug
            ]
            revisions.sort(key=lambda item: (item.created_at, str(item.id)), reverse=True)
            return revisions[:limit]

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
    ) -> WidgetRevision:
        async with self._lock:
            stored = self._revision(principal, revision_id)
            if stored.state is not expected:
                raise ConflictError(
                    f"revision {revision_id} is {stored.state}, not {expected}",
                    reason="revision_state_changed",
                )
            changed = _revised(
                stored,
                state=state,
                updated_at=updated_at,
                bundle=bundle,
                bundle_sha256=bundle_sha256,
                kit_api=kit_api,
                commit_sha=commit_sha,
                diagnostics=diagnostics,
            )
            self._revisions[_owner(principal)][revision_id] = changed
            return changed

    async def activate(
        self, principal: Principal, slug: str, revision_id: UUID, *, updated_at: datetime
    ) -> Widget:
        async with self._lock:
            widget = self._widget(principal, slug)
            target = self._revision(principal, revision_id)
            if target.slug != slug:
                raise NotFoundError(f"widget revision {revision_id} not found")
            if target.state not in ACTIVATABLE:
                raise ConflictError(
                    f"revision {revision_id} has no commit", reason="revision_not_committed"
                )
            revisions = self._revisions[_owner(principal)]
            previous = widget.live_revision_id
            if previous is not None and previous != revision_id and previous in revisions:
                revisions[previous] = revisions[previous].model_copy(
                    update={"state": RevisionState.SUPERSEDED, "updated_at": updated_at}
                )
            revisions[revision_id] = target.model_copy(
                update={"state": RevisionState.LIVE, "updated_at": updated_at}
            )
            live = widget.model_copy(
                update={
                    "state": WidgetState.ACTIVE,
                    "live_revision_id": revision_id,
                    "updated_at": updated_at,
                }
            )
            self._widgets[_owner(principal)][slug] = live
            return live

    async def mark_deleted(
        self, principal: Principal, slug: str, *, updated_at: datetime
    ) -> Widget:
        async with self._lock:
            deleted = self._widget(principal, slug).model_copy(
                update={"state": WidgetState.DELETED, "updated_at": updated_at}
            )
            self._widgets[_owner(principal)][slug] = deleted
            layouts = self._layouts.get(_owner(principal), {})
            for device_class, layout in list(layouts.items()):
                changed = _without(layout, slug, updated_at)
                if changed is not None:
                    layouts[device_class] = changed
            return deleted

    async def bundle(self, principal: Principal, bundle_sha256: str) -> str | None:
        async with self._lock:
            for revision in self._revisions.get(_owner(principal), {}).values():
                if revision.bundle_sha256 == bundle_sha256:
                    return revision.bundle
            return None

    async def get_settings(self, principal: Principal, slug: str) -> WidgetSettings | None:
        async with self._lock:
            return self._settings.get(_owner(principal), {}).get(slug)

    async def put_settings(self, settings: WidgetSettings) -> WidgetSettings:
        owner = Principal(tenant_id=settings.tenant_id, principal_id=settings.principal_id)
        async with self._lock:
            self._widget(owner, settings.slug)
            self._settings.setdefault(_owner(owner), {})[settings.slug] = settings
            return settings

    async def get_layout(self, principal: Principal, device_class: DeviceClass) -> Layout | None:
        async with self._lock:
            return self._layouts.get(_owner(principal), {}).get(device_class)

    async def put_layout(self, layout: Layout, *, expected_revision: int | None) -> Layout:
        async with self._lock:
            owned = self._layouts.setdefault((layout.tenant_id, layout.principal_id), {})
            current = owned.get(layout.device_class)
            current_revision = None if current is None else current.revision
            if current_revision != expected_revision:
                raise ConflictError("the layout changed", reason="layout_changed")
            stored = layout.model_copy(update={"revision": (expected_revision or 0) + 1})
            owned[layout.device_class] = stored
            return stored

    async def erase_principal(self, principal: Principal) -> int:
        async with self._lock:
            owner = _owner(principal)
            removed = 0
            for table in (self._widgets, self._revisions, self._settings, self._layouts):
                removed += len(table.pop(owner, {}))
            return removed


class InMemoryWidgetCommitOutbox:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, UUID], CommitOutboxEntry] = {}
        self._failures: dict[Owner, dict[UUID, ImportFailure]] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, entry: CommitOutboxEntry) -> CommitOutboxEntry:
        key = (entry.tenant_id, entry.principal_id, entry.revision_id)
        async with self._lock:
            if key in self._entries:
                raise ConflictError("the revision's commit is queued", reason="commit_queued")
            self._entries[key] = entry
            return entry

    async def due(self, tenant_id: str, *, now: datetime, limit: int) -> list[CommitOutboxEntry]:
        async with self._lock:
            due = [
                entry
                for entry in self._entries.values()
                if entry.tenant_id == tenant_id and entry.next_attempt_at <= now
            ]
            due.sort(
                key=lambda item: (item.next_attempt_at, item.created_at, str(item.revision_id))
            )
            return due[:limit]

    async def reschedule(
        self,
        principal: Principal,
        revision_id: UUID,
        *,
        attempts: int,
        next_attempt_at: datetime,
        last_error: str | None,
    ) -> CommitOutboxEntry:
        key = (*_owner(principal), revision_id)
        async with self._lock:
            stored = self._entries.get(key)
            if stored is None:
                raise NotFoundError(f"no queued commit for revision {revision_id}")
            changed = stored.model_copy(
                update={
                    "attempts": attempts,
                    "next_attempt_at": next_attempt_at,
                    "last_error": last_error,
                }
            )
            self._entries[key] = changed
            return changed

    async def complete(self, principal: Principal, revision_id: UUID) -> None:
        async with self._lock:
            self._entries.pop((*_owner(principal), revision_id), None)

    async def record_import_failure(self, failure: ImportFailure) -> ImportFailure:
        async with self._lock:
            self._failures.setdefault((failure.tenant_id, failure.principal_id), {})[failure.id] = (
                failure
            )
            return failure

    async def import_failures(
        self, principal: Principal, *, unseen_only: bool = False
    ) -> list[ImportFailure]:
        async with self._lock:
            failures = [
                item
                for item in self._failures.get(_owner(principal), {}).values()
                if not (unseen_only and item.seen)
            ]
            failures.sort(key=lambda item: (item.created_at, str(item.id)), reverse=True)
            return failures

    async def mark_seen(self, principal: Principal, failure_ids: Sequence[UUID]) -> int:
        async with self._lock:
            owned = self._failures.get(_owner(principal), {})
            marked = 0
            for failure_id in failure_ids:
                if failure_id in owned and not owned[failure_id].seen:
                    owned[failure_id] = owned[failure_id].model_copy(update={"seen": True})
                    marked += 1
            return marked

    async def erase_principal(self, principal: Principal) -> int:
        async with self._lock:
            owner = _owner(principal)
            entries = [key for key in self._entries if key[:2] == owner]
            for key in entries:
                del self._entries[key]
            return len(entries) + len(self._failures.pop(owner, {}))


class InMemoryFeedStore:
    def __init__(self) -> None:
        self._feeds: dict[Owner, dict[str, Feed]] = {}
        self._snapshots: dict[tuple[str, str, str], list[FeedSnapshot]] = {}
        self._lock = asyncio.Lock()

    def _feed(self, principal: Principal, feed_id: str) -> Feed:
        found = self._feeds.get(_owner(principal), {}).get(feed_id)
        if found is None:
            raise NotFoundError(f"feed {feed_id} not found")
        return found

    async def register(self, feed: Feed) -> tuple[Feed, bool]:
        async with self._lock:
            owned = self._feeds.setdefault((feed.tenant_id, feed.principal_id), {})
            if feed.id in owned:
                return owned[feed.id], False
            owned[feed.id] = feed
            return feed, True

    async def get(self, principal: Principal, feed_id: str) -> Feed | None:
        async with self._lock:
            return self._feeds.get(_owner(principal), {}).get(feed_id)

    async def list_feeds(self, principal: Principal) -> list[Feed]:
        async with self._lock:
            return sorted(self._feeds.get(_owner(principal), {}).values(), key=lambda item: item.id)

    async def replace_schema(
        self,
        principal: Principal,
        feed_id: str,
        *,
        schema: FeedSchemaDocument,
        updated_at: datetime,
    ) -> Feed:
        async with self._lock:
            stored = self._feed(principal, feed_id)
            replaced = Feed.model_validate(
                {
                    **stored.model_dump(by_alias=True),
                    "schema": dict(schema),
                    "schema_version": stored.schema_version + 1,
                    "updated_at": updated_at,
                }
            )
            self._feeds[_owner(principal)][feed_id] = replaced
            return replaced

    async def publish(self, snapshot: FeedSnapshot, *, history_max: int) -> FeedSnapshot:
        owner = Principal(tenant_id=snapshot.tenant_id, principal_id=snapshot.principal_id)
        async with self._lock:
            feed = self._feed(owner, snapshot.feed_id)
            if snapshot.schema_version != feed.schema_version:
                raise ConflictError("the feed's schema changed", reason="feed_schema_changed")
            history = self._snapshots.setdefault((*_owner(owner), snapshot.feed_id), [])
            sequence = history[-1].sequence + 1 if history else 1
            stored = snapshot.model_copy(update={"sequence": sequence})
            history.append(stored)
            del history[: max(0, len(history) - max(1, history_max))]
            return stored

    async def latest(
        self, principal: Principal, feed_ids: Sequence[str]
    ) -> dict[str, FeedSnapshot]:
        async with self._lock:
            latest: dict[str, FeedSnapshot] = {}
            for feed_id in feed_ids:
                history = self._snapshots.get((*_owner(principal), feed_id))
                if history:
                    latest[feed_id] = history[-1]
            return latest

    async def history(
        self, principal: Principal, feed_id: str, *, limit: int
    ) -> list[FeedSnapshot]:
        async with self._lock:
            history = self._snapshots.get((*_owner(principal), feed_id), [])
            return list(history[-limit:]) if limit > 0 else []

    async def prune_recorded_before(self, principal: Principal, *, before: datetime) -> int:
        async with self._lock:
            removed = 0
            for key, history in self._snapshots.items():
                if key[:2] != _owner(principal) or not history:
                    continue
                kept = [item for item in history[:-1] if item.recorded_at >= before] + history[-1:]
                removed += len(history) - len(kept)
                history[:] = kept
            return removed

    async def erase_principal(self, principal: Principal) -> int:
        async with self._lock:
            owner = _owner(principal)
            removed = len(self._feeds.pop(owner, {}))
            for key in [key for key in self._snapshots if key[:2] == owner]:
                removed += len(self._snapshots.pop(key))
            return removed


class InMemoryCockpitEventLog:
    def __init__(self) -> None:
        self._events: list[CockpitEvent] = []
        self._sequence = itertools.count(1)
        self._lock = asyncio.Lock()

    def _owned(self, principal: Principal) -> list[CockpitEvent]:
        owner = _owner(principal)
        return [item for item in self._events if (item.tenant_id, item.principal_id) == owner]

    async def append(
        self,
        principal: Principal,
        *,
        kind: CockpitEventKind,
        subject: str,
        occurred_at: datetime,
    ) -> CockpitEvent:
        async with self._lock:
            event = CockpitEvent(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                sequence=next(self._sequence),
                kind=kind,
                subject=subject,
                occurred_at=occurred_at,
            )
            self._events.append(event)
            return event

    async def after(self, principal: Principal, *, sequence: int, limit: int) -> list[CockpitEvent]:
        async with self._lock:
            return [item for item in self._owned(principal) if item.sequence > sequence][:limit]

    async def oldest_sequence(self, principal: Principal) -> int | None:
        async with self._lock:
            owned = self._owned(principal)
            return owned[0].sequence if owned else None

    async def latest_sequence(self, principal: Principal) -> int | None:
        async with self._lock:
            owned = self._owned(principal)
            return owned[-1].sequence if owned else None

    async def prune_before(self, tenant_id: str, *, before: datetime) -> int:
        async with self._lock:
            kept = [
                item
                for item in self._events
                if item.tenant_id != tenant_id or item.occurred_at >= before
            ]
            removed = len(self._events) - len(kept)
            self._events = kept
            return removed

    async def erase_principal(self, principal: Principal) -> int:
        async with self._lock:
            owner = _owner(principal)
            kept = [item for item in self._events if (item.tenant_id, item.principal_id) != owner]
            removed = len(self._events) - len(kept)
            self._events = kept
            return removed


# PostgreSQL ------------------------------------------------------------------


def _rowcount(result: Any) -> int:
    return int(result.rowcount or 0)


def _owned_by(table: Any, principal: Principal) -> list[ColumnElement[bool]]:
    return [table.tenant_id == principal.tenant_id, table.principal_id == principal.principal_id]


def _widget_from_row(row: CockpitWidgetRow) -> Widget:
    return Widget(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        slug=row.slug,
        state=WidgetState(row.state),
        live_revision_id=row.live_revision_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision_from_row(row: CockpitWidgetRevisionRow) -> WidgetRevision:
    return WidgetRevision(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        slug=row.slug,
        source=RevisionSource(row.source),
        state=RevisionState(row.state),
        files=dict(row.files),
        diagnostics=tuple(BuildDiagnostic.model_validate(item) for item in row.diagnostics),
        bundle=row.bundle,
        bundle_sha256=row.bundle_sha256,
        kit_api=row.kit_api,
        commit_sha=row.commit_sha,
        origin_run_id=row.origin_run_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision_values(revision: WidgetRevision) -> dict[str, Any]:
    return {
        "tenant_id": revision.tenant_id,
        "principal_id": revision.principal_id,
        "id": revision.id,
        "slug": revision.slug,
        "source": revision.source.value,
        "state": revision.state.value,
        "files": dict(revision.files),
        "diagnostics": [item.model_dump() for item in revision.diagnostics],
        "bundle": revision.bundle,
        "bundle_sha256": revision.bundle_sha256,
        "kit_api": revision.kit_api,
        "commit_sha": revision.commit_sha,
        "origin_run_id": revision.origin_run_id,
        "created_at": revision.created_at,
        "updated_at": revision.updated_at,
    }


def _layout_from_row(row: CockpitLayoutRow) -> Layout:
    return Layout(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        device_class=DeviceClass(row.device_class),
        placements=tuple(Placement.model_validate(item) for item in row.placements),
        revision=row.revision,
        updated_at=row.updated_at,
    )


class PostgresWidgetStore:
    """Widgets in PostgreSQL; the unit of work owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _widget_row(
        self, principal: Principal, slug: str, *, lock: bool = False
    ) -> CockpitWidgetRow:
        query = select(CockpitWidgetRow).where(
            *_owned_by(CockpitWidgetRow, principal), CockpitWidgetRow.slug == slug
        )
        row = (
            await self._session.execute(query.with_for_update() if lock else query)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"widget {slug} not found")
        return row

    async def _revision_row(
        self, principal: Principal, revision_id: UUID, *, lock: bool = False
    ) -> CockpitWidgetRevisionRow:
        query = select(CockpitWidgetRevisionRow).where(
            *_owned_by(CockpitWidgetRevisionRow, principal),
            CockpitWidgetRevisionRow.id == revision_id,
        )
        row = (
            await self._session.execute(query.with_for_update() if lock else query)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"widget revision {revision_id} not found")
        return row

    async def _insert(self, statement: Any, *, reason: str, message: str) -> None:
        try:
            async with self._session.begin_nested():
                await self._session.execute(statement)
        except IntegrityError as error:
            raise ConflictError(message, reason=reason) from error

    async def create_widget(self, widget: Widget) -> Widget:
        await self._insert(
            insert(CockpitWidgetRow).values(
                tenant_id=widget.tenant_id,
                principal_id=widget.principal_id,
                slug=widget.slug,
                state=widget.state.value,
                live_revision_id=widget.live_revision_id,
                created_at=widget.created_at,
                updated_at=widget.updated_at,
            ),
            reason="widget_exists",
            message=f"widget {widget.slug} exists",
        )
        return widget

    async def get_widget(self, principal: Principal, slug: str) -> Widget:
        return _widget_from_row(await self._widget_row(principal, slug))

    async def list_widgets(
        self, principal: Principal, *, include_deleted: bool = False
    ) -> list[Widget]:
        query = select(CockpitWidgetRow).where(*_owned_by(CockpitWidgetRow, principal))
        if not include_deleted:
            query = query.where(CockpitWidgetRow.state == WidgetState.ACTIVE.value)
        rows = (await self._session.execute(query.order_by(CockpitWidgetRow.slug))).scalars()
        return [_widget_from_row(row) for row in rows]

    async def add_revision(self, revision: WidgetRevision) -> WidgetRevision:
        owner = Principal(tenant_id=revision.tenant_id, principal_id=revision.principal_id)
        await self._widget_row(owner, revision.slug)
        await self._insert(
            insert(CockpitWidgetRevisionRow).values(**_revision_values(revision)),
            reason="revision_exists",
            message=f"revision {revision.id} exists",
        )
        return revision

    async def get_revision(self, principal: Principal, revision_id: UUID) -> WidgetRevision:
        return _revision_from_row(await self._revision_row(principal, revision_id))

    async def list_revisions(
        self, principal: Principal, slug: str, *, limit: int
    ) -> list[WidgetRevision]:
        rows = (
            await self._session.execute(
                select(CockpitWidgetRevisionRow)
                .where(
                    *_owned_by(CockpitWidgetRevisionRow, principal),
                    CockpitWidgetRevisionRow.slug == slug,
                )
                .order_by(
                    CockpitWidgetRevisionRow.created_at.desc(), CockpitWidgetRevisionRow.id.desc()
                )
                .limit(limit)
            )
        ).scalars()
        return [_revision_from_row(row) for row in rows]

    async def _store_revision(self, revision: WidgetRevision) -> None:
        values = _revision_values(revision)
        await self._session.execute(
            update(CockpitWidgetRevisionRow)
            .where(
                CockpitWidgetRevisionRow.tenant_id == revision.tenant_id,
                CockpitWidgetRevisionRow.principal_id == revision.principal_id,
                CockpitWidgetRevisionRow.id == revision.id,
            )
            .values(
                {
                    key: value
                    for key, value in values.items()
                    if key not in {"tenant_id", "principal_id", "id"}
                }
            )
        )

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
    ) -> WidgetRevision:
        stored = _revision_from_row(await self._revision_row(principal, revision_id, lock=True))
        if stored.state is not expected:
            raise ConflictError(
                f"revision {revision_id} is {stored.state}, not {expected}",
                reason="revision_state_changed",
            )
        changed = _revised(
            stored,
            state=state,
            updated_at=updated_at,
            bundle=bundle,
            bundle_sha256=bundle_sha256,
            kit_api=kit_api,
            commit_sha=commit_sha,
            diagnostics=diagnostics,
        )
        await self._store_revision(changed)
        return changed

    async def activate(
        self, principal: Principal, slug: str, revision_id: UUID, *, updated_at: datetime
    ) -> Widget:
        widget = await self._widget_row(principal, slug, lock=True)
        target = _revision_from_row(await self._revision_row(principal, revision_id, lock=True))
        if target.slug != slug:
            raise NotFoundError(f"widget revision {revision_id} not found")
        if target.state not in ACTIVATABLE:
            raise ConflictError(
                f"revision {revision_id} has no commit", reason="revision_not_committed"
            )
        previous = widget.live_revision_id
        if previous is not None and previous != revision_id:
            await self._session.execute(
                update(CockpitWidgetRevisionRow)
                .where(
                    *_owned_by(CockpitWidgetRevisionRow, principal),
                    CockpitWidgetRevisionRow.id == previous,
                )
                .values(state=RevisionState.SUPERSEDED.value, updated_at=updated_at)
            )
        await self._session.execute(
            update(CockpitWidgetRevisionRow)
            .where(
                *_owned_by(CockpitWidgetRevisionRow, principal),
                CockpitWidgetRevisionRow.id == revision_id,
            )
            .values(state=RevisionState.LIVE.value, updated_at=updated_at)
        )
        widget.state = WidgetState.ACTIVE.value
        widget.live_revision_id = revision_id
        widget.updated_at = updated_at
        await self._session.flush()
        return _widget_from_row(widget)

    async def mark_deleted(
        self, principal: Principal, slug: str, *, updated_at: datetime
    ) -> Widget:
        widget = await self._widget_row(principal, slug, lock=True)
        widget.state = WidgetState.DELETED.value
        widget.updated_at = updated_at
        layouts = (
            await self._session.execute(
                select(CockpitLayoutRow)
                .where(*_owned_by(CockpitLayoutRow, principal))
                .with_for_update()
            )
        ).scalars()
        for row in layouts:
            changed = _without(_layout_from_row(row), slug, updated_at)
            if changed is not None:
                row.placements = [item.model_dump() for item in changed.placements]
                row.revision = changed.revision
                row.updated_at = changed.updated_at
        await self._session.flush()
        return _widget_from_row(widget)

    async def bundle(self, principal: Principal, bundle_sha256: str) -> str | None:
        return (
            await self._session.execute(
                select(CockpitWidgetRevisionRow.bundle)
                .where(
                    *_owned_by(CockpitWidgetRevisionRow, principal),
                    CockpitWidgetRevisionRow.bundle_sha256 == bundle_sha256,
                    CockpitWidgetRevisionRow.bundle.is_not(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    async def get_settings(self, principal: Principal, slug: str) -> WidgetSettings | None:
        row = (
            await self._session.execute(
                select(CockpitWidgetSettingsRow).where(
                    *_owned_by(CockpitWidgetSettingsRow, principal),
                    CockpitWidgetSettingsRow.slug == slug,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return WidgetSettings(
            tenant_id=row.tenant_id,
            principal_id=row.principal_id,
            slug=row.slug,
            values=dict(row.values),
            updated_at=row.updated_at,
        )

    async def put_settings(self, settings: WidgetSettings) -> WidgetSettings:
        owner = Principal(tenant_id=settings.tenant_id, principal_id=settings.principal_id)
        await self._widget_row(owner, settings.slug)
        statement = pg_insert(CockpitWidgetSettingsRow).values(
            tenant_id=settings.tenant_id,
            principal_id=settings.principal_id,
            slug=settings.slug,
            values=dict(settings.values),
            updated_at=settings.updated_at,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "principal_id", "slug"],
                set_={
                    "values": statement.excluded["values"],
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
        return settings

    async def get_layout(self, principal: Principal, device_class: DeviceClass) -> Layout | None:
        row = (
            await self._session.execute(
                select(CockpitLayoutRow).where(
                    *_owned_by(CockpitLayoutRow, principal),
                    CockpitLayoutRow.device_class == device_class.value,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _layout_from_row(row)

    async def put_layout(self, layout: Layout, *, expected_revision: int | None) -> Layout:
        stored = layout.model_copy(update={"revision": (expected_revision or 0) + 1})
        placements = [item.model_dump() for item in stored.placements]
        if expected_revision is None:
            await self._insert(
                insert(CockpitLayoutRow).values(
                    tenant_id=stored.tenant_id,
                    principal_id=stored.principal_id,
                    device_class=stored.device_class.value,
                    placements=placements,
                    revision=stored.revision,
                    updated_at=stored.updated_at,
                ),
                reason="layout_changed",
                message="the layout changed",
            )
            return stored
        result = await self._session.execute(
            update(CockpitLayoutRow)
            .where(
                CockpitLayoutRow.tenant_id == stored.tenant_id,
                CockpitLayoutRow.principal_id == stored.principal_id,
                CockpitLayoutRow.device_class == stored.device_class.value,
                CockpitLayoutRow.revision == expected_revision,
            )
            .values(placements=placements, revision=stored.revision, updated_at=stored.updated_at)
        )
        if _rowcount(result) != 1:
            raise ConflictError("the layout changed", reason="layout_changed")
        return stored

    async def erase_principal(self, principal: Principal) -> int:
        removed = 0
        for table in (
            CockpitWidgetSettingsRow,
            CockpitWidgetRevisionRow,
            CockpitWidgetRow,
            CockpitLayoutRow,
        ):
            removed += _rowcount(
                await self._session.execute(delete(table).where(*_owned_by(table, principal)))
            )
        return removed


def _entry_from_row(row: CockpitCommitOutboxRow) -> CommitOutboxEntry:
    return CommitOutboxEntry(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        revision_id=row.revision_id,
        slug=row.slug,
        operation=OutboxOperation(row.operation),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        expected_parent_sha=row.expected_parent_sha,
        created_at=row.created_at,
    )


def _failure_from_row(row: CockpitImportFailureRow) -> ImportFailure:
    return ImportFailure(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        commit_sha=row.commit_sha,
        slug=row.slug,
        diagnostics=tuple(BuildDiagnostic.model_validate(item) for item in row.diagnostics),
        seen=row.seen,
        created_at=row.created_at,
    )


class PostgresWidgetCommitOutbox:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, entry: CommitOutboxEntry) -> CommitOutboxEntry:
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    insert(CockpitCommitOutboxRow).values(
                        tenant_id=entry.tenant_id,
                        principal_id=entry.principal_id,
                        revision_id=entry.revision_id,
                        slug=entry.slug,
                        operation=entry.operation.value,
                        attempts=entry.attempts,
                        next_attempt_at=entry.next_attempt_at,
                        last_error=entry.last_error,
                        expected_parent_sha=entry.expected_parent_sha,
                        created_at=entry.created_at,
                    )
                )
        except IntegrityError as error:
            raise ConflictError(
                "the revision's commit is queued", reason="commit_queued"
            ) from error
        return entry

    async def due(self, tenant_id: str, *, now: datetime, limit: int) -> list[CommitOutboxEntry]:
        rows = (
            await self._session.execute(
                select(CockpitCommitOutboxRow)
                .where(
                    CockpitCommitOutboxRow.tenant_id == tenant_id,
                    CockpitCommitOutboxRow.next_attempt_at <= now,
                )
                .order_by(
                    CockpitCommitOutboxRow.next_attempt_at,
                    CockpitCommitOutboxRow.created_at,
                    CockpitCommitOutboxRow.revision_id,
                )
                .limit(limit)
            )
        ).scalars()
        return [_entry_from_row(row) for row in rows]

    async def reschedule(
        self,
        principal: Principal,
        revision_id: UUID,
        *,
        attempts: int,
        next_attempt_at: datetime,
        last_error: str | None,
    ) -> CommitOutboxEntry:
        row = (
            await self._session.execute(
                update(CockpitCommitOutboxRow)
                .where(
                    *_owned_by(CockpitCommitOutboxRow, principal),
                    CockpitCommitOutboxRow.revision_id == revision_id,
                )
                .values(attempts=attempts, next_attempt_at=next_attempt_at, last_error=last_error)
                .returning(CockpitCommitOutboxRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"no queued commit for revision {revision_id}")
        return _entry_from_row(row)

    async def complete(self, principal: Principal, revision_id: UUID) -> None:
        await self._session.execute(
            delete(CockpitCommitOutboxRow).where(
                *_owned_by(CockpitCommitOutboxRow, principal),
                CockpitCommitOutboxRow.revision_id == revision_id,
            )
        )

    async def record_import_failure(self, failure: ImportFailure) -> ImportFailure:
        await self._session.execute(
            insert(CockpitImportFailureRow).values(
                tenant_id=failure.tenant_id,
                principal_id=failure.principal_id,
                id=failure.id,
                commit_sha=failure.commit_sha,
                slug=failure.slug,
                diagnostics=[item.model_dump() for item in failure.diagnostics],
                seen=failure.seen,
                created_at=failure.created_at,
            )
        )
        return failure

    async def import_failures(
        self, principal: Principal, *, unseen_only: bool = False
    ) -> list[ImportFailure]:
        query = select(CockpitImportFailureRow).where(
            *_owned_by(CockpitImportFailureRow, principal)
        )
        if unseen_only:
            query = query.where(CockpitImportFailureRow.seen.is_(False))
        rows = (
            await self._session.execute(
                query.order_by(
                    CockpitImportFailureRow.created_at.desc(), CockpitImportFailureRow.id.desc()
                )
            )
        ).scalars()
        return [_failure_from_row(row) for row in rows]

    async def mark_seen(self, principal: Principal, failure_ids: Sequence[UUID]) -> int:
        if not failure_ids:
            return 0
        result = await self._session.execute(
            update(CockpitImportFailureRow)
            .where(
                *_owned_by(CockpitImportFailureRow, principal),
                CockpitImportFailureRow.id.in_(list(failure_ids)),
                CockpitImportFailureRow.seen.is_(False),
            )
            .values(seen=True)
        )
        return _rowcount(result)

    async def erase_principal(self, principal: Principal) -> int:
        removed = 0
        for table in (CockpitCommitOutboxRow, CockpitImportFailureRow):
            removed += _rowcount(
                await self._session.execute(delete(table).where(*_owned_by(table, principal)))
            )
        return removed


def _feed_from_row(row: CockpitFeedRow) -> Feed:
    return Feed.model_validate(
        {
            "tenant_id": row.tenant_id,
            "principal_id": row.principal_id,
            "id": row.id,
            "schema": row.schema,
            "schema_version": row.schema_version,
            "trust": row.trust,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _snapshot_from_row(row: CockpitFeedSnapshotRow) -> FeedSnapshot:
    return FeedSnapshot(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        feed_id=row.feed_id,
        sequence=row.sequence,
        observed_at=row.observed_at,
        expected_interval_seconds=row.expected_interval_seconds,
        error=row.error,
        data=row.data,
        schema_version=row.schema_version,
        recorded_at=row.recorded_at,
    )


class PostgresFeedStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _feed_query(self, principal: Principal, feed_id: str) -> Select[tuple[CockpitFeedRow]]:
        return select(CockpitFeedRow).where(
            *_owned_by(CockpitFeedRow, principal), CockpitFeedRow.id == feed_id
        )

    async def register(self, feed: Feed) -> tuple[Feed, bool]:
        owner = Principal(tenant_id=feed.tenant_id, principal_id=feed.principal_id)
        existing = await self.get(owner, feed.id)
        if existing is not None:
            return existing, False
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    insert(CockpitFeedRow).values(
                        tenant_id=feed.tenant_id,
                        principal_id=feed.principal_id,
                        id=feed.id,
                        schema=feed.schema_document,
                        schema_version=feed.schema_version,
                        trust=(feed.trust or FeedTrust.UNTRUSTED).value,
                        created_at=feed.created_at,
                        updated_at=feed.updated_at,
                    )
                )
        except IntegrityError:
            raced = await self.get(owner, feed.id)
            if raced is None:
                raise
            return raced, False
        return feed, True

    async def get(self, principal: Principal, feed_id: str) -> Feed | None:
        row = (
            await self._session.execute(self._feed_query(principal, feed_id))
        ).scalar_one_or_none()
        return None if row is None else _feed_from_row(row)

    async def list_feeds(self, principal: Principal) -> list[Feed]:
        rows = (
            await self._session.execute(
                select(CockpitFeedRow)
                .where(*_owned_by(CockpitFeedRow, principal))
                .order_by(CockpitFeedRow.id)
            )
        ).scalars()
        return [_feed_from_row(row) for row in rows]

    async def _locked(self, principal: Principal, feed_id: str) -> CockpitFeedRow:
        row = (
            await self._session.execute(self._feed_query(principal, feed_id).with_for_update())
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"feed {feed_id} not found")
        return row

    async def replace_schema(
        self,
        principal: Principal,
        feed_id: str,
        *,
        schema: FeedSchemaDocument,
        updated_at: datetime,
    ) -> Feed:
        row = await self._locked(principal, feed_id)
        row.schema = dict(schema)
        row.schema_version += 1
        row.updated_at = updated_at
        await self._session.flush()
        return _feed_from_row(row)

    async def publish(self, snapshot: FeedSnapshot, *, history_max: int) -> FeedSnapshot:
        owner = Principal(tenant_id=snapshot.tenant_id, principal_id=snapshot.principal_id)
        feed = await self._locked(owner, snapshot.feed_id)
        if snapshot.schema_version != feed.schema_version:
            raise ConflictError("the feed's schema changed", reason="feed_schema_changed")
        scope = [
            *_owned_by(CockpitFeedSnapshotRow, owner),
            CockpitFeedSnapshotRow.feed_id == snapshot.feed_id,
        ]
        last = (
            await self._session.execute(
                select(func.max(CockpitFeedSnapshotRow.sequence)).where(*scope)
            )
        ).scalar_one()
        stored = snapshot.model_copy(update={"sequence": (last or 0) + 1})
        await self._session.execute(
            insert(CockpitFeedSnapshotRow).values(
                tenant_id=stored.tenant_id,
                principal_id=stored.principal_id,
                feed_id=stored.feed_id,
                sequence=stored.sequence,
                observed_at=stored.observed_at,
                expected_interval_seconds=stored.expected_interval_seconds,
                error=stored.error,
                data=stored.data,
                schema_version=stored.schema_version,
                recorded_at=stored.recorded_at,
            )
        )
        await self._session.execute(
            delete(CockpitFeedSnapshotRow).where(
                *scope, CockpitFeedSnapshotRow.sequence <= stored.sequence - max(1, history_max)
            )
        )
        return stored

    async def latest(
        self, principal: Principal, feed_ids: Sequence[str]
    ) -> dict[str, FeedSnapshot]:
        if not feed_ids:
            return {}
        rows = (
            await self._session.execute(
                select(CockpitFeedSnapshotRow)
                .where(
                    *_owned_by(CockpitFeedSnapshotRow, principal),
                    CockpitFeedSnapshotRow.feed_id.in_(list(feed_ids)),
                )
                .order_by(CockpitFeedSnapshotRow.feed_id, CockpitFeedSnapshotRow.sequence.desc())
                .distinct(CockpitFeedSnapshotRow.feed_id)
            )
        ).scalars()
        found = {row.feed_id: _snapshot_from_row(row) for row in rows}
        return {feed_id: found[feed_id] for feed_id in feed_ids if feed_id in found}

    async def history(
        self, principal: Principal, feed_id: str, *, limit: int
    ) -> list[FeedSnapshot]:
        if limit <= 0:
            return []
        rows = (
            await self._session.execute(
                select(CockpitFeedSnapshotRow)
                .where(
                    *_owned_by(CockpitFeedSnapshotRow, principal),
                    CockpitFeedSnapshotRow.feed_id == feed_id,
                )
                .order_by(CockpitFeedSnapshotRow.sequence.desc())
                .limit(limit)
            )
        ).scalars()
        return [_snapshot_from_row(row) for row in reversed(list(rows))]

    async def prune_recorded_before(self, principal: Principal, *, before: datetime) -> int:
        newest = aliased(CockpitFeedSnapshotRow)
        latest_sequence = (
            select(func.max(newest.sequence))
            .where(
                newest.tenant_id == CockpitFeedSnapshotRow.tenant_id,
                newest.principal_id == CockpitFeedSnapshotRow.principal_id,
                newest.feed_id == CockpitFeedSnapshotRow.feed_id,
            )
            .scalar_subquery()
        )
        result = await self._session.execute(
            delete(CockpitFeedSnapshotRow).where(
                *_owned_by(CockpitFeedSnapshotRow, principal),
                CockpitFeedSnapshotRow.recorded_at < before,
                CockpitFeedSnapshotRow.sequence < latest_sequence,
            )
        )
        return _rowcount(result)

    async def erase_principal(self, principal: Principal) -> int:
        removed = 0
        for table in (CockpitFeedSnapshotRow, CockpitFeedRow):
            removed += _rowcount(
                await self._session.execute(delete(table).where(*_owned_by(table, principal)))
            )
        return removed


def _event_from_row(row: CockpitEventRow) -> CockpitEvent:
    return CockpitEvent(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        sequence=row.sequence,
        kind=CockpitEventKind(row.kind),
        subject=row.subject,
        occurred_at=row.occurred_at,
    )


class PostgresCockpitEventLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        principal: Principal,
        *,
        kind: CockpitEventKind,
        subject: str,
        occurred_at: datetime,
    ) -> CockpitEvent:
        sequence = (
            await self._session.execute(
                insert(CockpitEventRow)
                .values(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    kind=kind.value,
                    subject=subject,
                    occurred_at=occurred_at,
                )
                .returning(CockpitEventRow.sequence)
            )
        ).scalar_one()
        # Delivered on commit with the row; readers fetch the row, so only the kind and
        # sequence travel (docs/plan/cockpit.md, the stream).
        await self._session.execute(
            select(
                func.pg_notify(
                    cockpit_channel(principal.tenant_id),
                    json.dumps({"kind": "cockpit", "sequence": sequence}),
                )
            )
        )
        return CockpitEvent(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            sequence=sequence,
            kind=kind,
            subject=subject,
            occurred_at=occurred_at,
        )

    async def after(self, principal: Principal, *, sequence: int, limit: int) -> list[CockpitEvent]:
        rows = (
            await self._session.execute(
                select(CockpitEventRow)
                .where(*_owned_by(CockpitEventRow, principal), CockpitEventRow.sequence > sequence)
                .order_by(CockpitEventRow.sequence)
                .limit(limit)
            )
        ).scalars()
        return [_event_from_row(row) for row in rows]

    async def _bound(self, principal: Principal, aggregate: Any) -> int | None:
        value = (
            await self._session.execute(
                select(aggregate(CockpitEventRow.sequence)).where(
                    *_owned_by(CockpitEventRow, principal)
                )
            )
        ).scalar_one()
        return None if value is None else int(value)

    async def oldest_sequence(self, principal: Principal) -> int | None:
        return await self._bound(principal, func.min)

    async def latest_sequence(self, principal: Principal) -> int | None:
        return await self._bound(principal, func.max)

    async def prune_before(self, tenant_id: str, *, before: datetime) -> int:
        result = await self._session.execute(
            delete(CockpitEventRow).where(
                CockpitEventRow.tenant_id == tenant_id, CockpitEventRow.occurred_at < before
            )
        )
        return _rowcount(result)

    async def erase_principal(self, principal: Principal) -> int:
        result = await self._session.execute(
            delete(CockpitEventRow).where(*_owned_by(CockpitEventRow, principal))
        )
        return _rowcount(result)
