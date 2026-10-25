"""The Cockpit board the routes read and the owner's writes (Milestone 35).

docs/plan/cockpit.md, the routes and the stream. The snapshot is one unit of
work's view of the board with the stream cursor it is consistent with; the
stream then replays `cockpit_events` after a cursor, woken by the tenant's
channel and polling when a notification is missed. A resume point the event
log no longer covers gets `stream.reset`, and every stream ends at its
lifetime so a revoked token takes effect on reconnect.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.application.cockpit import CockpitPublication
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    CockpitEventKind,
    DeviceClass,
    Layout,
    Widget,
    WidgetRevision,
    WidgetSettings,
    WidgetState,
    cockpit_channel,
    feed_trust,
    is_bundle_digest,
)
from agent_core.domain.cockpit_views import (
    CockpitChangeView,
    CockpitFeedEntryView,
    CockpitFeedView,
    CockpitImportFailureView,
    CockpitLayoutView,
    CockpitLayoutWrite,
    CockpitRevisionView,
    CockpitRuntimeView,
    CockpitSettingsView,
    CockpitSnapshotView,
    CockpitWidgetDetailView,
    CockpitWidgetView,
)
from agent_core.domain.errors import (
    CockpitBuildUnavailable,
    CockpitValidationError,
    NotFoundError,
)
from agent_core.domain.views import PersistedStreamFrame, StreamFrame, TransientStreamFrame
from agent_core.ports.cockpit import (
    CockpitAsset,
    CockpitRuntimeAssets,
    JsonSchemaCheck,
    WidgetBuildFailedError,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.live_events import LiveEventBroadcaster
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

# Readers keep polling at the run stream's interval when a notification is missed.
_IDLE_POLL_SECONDS = 5.0
_REPLAY_PAGE = 100
_DETAIL_REVISIONS = 20
_BUNDLE_CONTENT_TYPE = "text/javascript; charset=utf-8"


def _manifest(revision: WidgetRevision) -> dict[str, Any]:
    try:
        manifest = json.loads(revision.files.get("widget.json", "{}"))
    except ValueError:
        return {}
    return manifest if isinstance(manifest, dict) else {}


class CockpitBoard:
    """The board, its stream, and the owner's settings, layout and widget changes."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        live_events: LiveEventBroadcaster,
        publication: CockpitPublication,
        runtime: CockpitRuntimeAssets,
        settings_schema: JsonSchemaCheck,
        stream_max_seconds: int,
        repository: str | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._live_events = live_events
        self._publication = publication
        self._runtime = runtime
        self._settings_schema = settings_schema
        self._stream_max = timedelta(seconds=stream_max_seconds)
        self._repository = repository

    def _commit_url(self, sha: str | None) -> str | None:
        if sha is None or self._repository is None:
            return None
        return f"https://github.com/{self._repository}/commit/{sha}"

    @staticmethod
    async def _live(
        uow: RepositoryUnitOfWork, principal: Principal, slug: str
    ) -> tuple[Widget, WidgetRevision]:
        widget = await uow.cockpit_widgets.get_widget(principal, slug)
        if widget.state is not WidgetState.ACTIVE or widget.live_revision_id is None:
            raise NotFoundError(f"widget {slug} not found")
        return widget, await uow.cockpit_widgets.get_revision(principal, widget.live_revision_id)

    async def snapshot(self, principal: Principal) -> CockpitSnapshotView:
        require_scope(principal, "cockpit.read")
        async with self._uow_factory() as uow:
            cursor = await uow.cockpit_events.latest_sequence(principal) or 0
            widgets: list[CockpitWidgetView] = []
            depths: dict[str, int] = {}
            for widget in await uow.cockpit_widgets.list_widgets(principal):
                if widget.live_revision_id is None:
                    continue
                live = await uow.cockpit_widgets.get_revision(principal, widget.live_revision_id)
                if live.bundle_sha256 is None or live.kit_api is None:
                    continue
                manifest = _manifest(live)
                for declared in manifest.get("feeds", []) or []:
                    if isinstance(declared, dict) and isinstance(declared.get("id"), str):
                        history = declared.get("history", 0)
                        depth = history if isinstance(history, int) and history > 0 else 0
                        depths[declared["id"]] = max(depths.get(declared["id"], 0), depth)
                settings = await uow.cockpit_widgets.get_settings(principal, widget.slug)
                widgets.append(
                    CockpitWidgetView(
                        slug=widget.slug,
                        revision_id=live.id,
                        bundle_sha256=live.bundle_sha256,
                        kit_api=live.kit_api,
                        manifest=manifest,
                        settings=dict(settings.values) if settings is not None else {},
                    )
                )
            layouts: dict[DeviceClass, CockpitLayoutView] = {}
            for device_class in DeviceClass:
                layout = await uow.cockpit_widgets.get_layout(principal, device_class)
                if layout is not None:
                    layouts[device_class] = CockpitLayoutView(
                        revision=layout.revision, placements=layout.placements
                    )
            current = await uow.cockpit_feeds.latest(principal, sorted(depths))
            feeds: dict[str, CockpitFeedView] = {}
            for feed_id, depth in sorted(depths.items()):
                latest = current.get(feed_id)
                earlier = (
                    await uow.cockpit_feeds.history(principal, feed_id, limit=depth + 1)
                    if depth and latest is not None
                    else []
                )
                feeds[feed_id] = CockpitFeedView(
                    trust=feed_trust(feed_id),
                    current=None if latest is None else self._entry(latest),
                    history=tuple(
                        self._entry(item)
                        for item in earlier
                        if latest is None or item.sequence != latest.sequence
                    )[-depth:]
                    if depth
                    else (),
                )
        runtime = self._runtime.manifest()
        return CockpitSnapshotView(
            cursor=cursor,
            runtime=None
            if runtime is None
            else CockpitRuntimeView(
                kit_api=runtime.kit_api, entry=runtime.entry, assets=dict(runtime.assets)
            ),
            widgets=tuple(widgets),
            layouts=layouts,
            feeds=feeds,
        )

    @staticmethod
    def _entry(snapshot: Any) -> CockpitFeedEntryView:
        return CockpitFeedEntryView(
            observed_at=snapshot.observed_at,
            expected_interval_seconds=snapshot.expected_interval_seconds,
            error=snapshot.error,
            data=snapshot.data,
            schema_version=snapshot.schema_version,
        )

    async def asset(self, principal: Principal, sha256: str) -> CockpitAsset:
        require_scope(principal, "cockpit.read")
        if not is_bundle_digest(sha256):
            raise NotFoundError("asset not found")
        async with self._uow_factory() as uow:
            bundle = await uow.cockpit_widgets.bundle(principal, sha256)
        if bundle is not None:
            return CockpitAsset(bundle.encode("utf-8"), _BUNDLE_CONTENT_TYPE)
        runtime = self._runtime.asset(sha256)
        if runtime is None:
            raise NotFoundError("asset not found")
        return runtime

    async def widget(self, principal: Principal, slug: str) -> CockpitWidgetDetailView:
        require_scope(principal, "cockpit.read")
        async with self._uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(principal, slug)
            revisions = await uow.cockpit_widgets.list_revisions(
                principal, slug, limit=_DETAIL_REVISIONS
            )
            live = (
                None
                if widget.live_revision_id is None
                else await uow.cockpit_widgets.get_revision(principal, widget.live_revision_id)
            )
            settings = await uow.cockpit_widgets.get_settings(principal, slug)
            failures = [
                failure
                for failure in await uow.cockpit_outbox.import_failures(principal)
                if failure.slug == slug
            ]
        schema = None if live is None else _manifest(live).get("settings")
        return CockpitWidgetDetailView(
            slug=widget.slug,
            state=widget.state,
            live_revision_id=widget.live_revision_id,
            settings_schema=schema if isinstance(schema, dict) else None,
            settings=dict(settings.values) if settings is not None else {},
            revisions=tuple(
                CockpitRevisionView(
                    id=revision.id,
                    state=revision.state,
                    source=revision.source,
                    commit_sha=revision.commit_sha,
                    commit_url=self._commit_url(revision.commit_sha),
                    diagnostics=revision.diagnostics,
                    created_at=revision.created_at,
                    updated_at=revision.updated_at,
                )
                for revision in revisions
            ),
            import_failures=tuple(
                CockpitImportFailureView(
                    id=failure.id,
                    commit_sha=failure.commit_sha,
                    commit_url=self._commit_url(failure.commit_sha),
                    diagnostics=failure.diagnostics,
                    seen=failure.seen,
                    created_at=failure.created_at,
                )
                for failure in failures
            ),
        )

    async def put_settings(
        self, principal: Principal, slug: str, values: Mapping[str, Any]
    ) -> CockpitSettingsView:
        """Replace a live widget's private settings, checked against its manifest's schema."""

        require_scope(principal, "cockpit.write")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            _widget, live = await self._live(uow, principal, slug)
            schema = _manifest(live).get("settings")
            if not isinstance(schema, dict):
                raise CockpitValidationError(f"{slug} declares no settings")
            problems = self._settings_schema.violations(schema, values)
            if problems:
                raise CockpitValidationError(f"the settings do not fit {slug}: {problems[0]}")
            try:
                settings = WidgetSettings(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    slug=slug,
                    values=dict(values),
                    updated_at=now,
                )
            except ValueError as error:
                raise CockpitValidationError(f"the settings are out of bounds: {error}") from error
            stored = await uow.cockpit_widgets.put_settings(settings)
            await uow.cockpit_events.append(
                principal, kind=CockpitEventKind.SETTINGS_UPDATED, subject=slug, occurred_at=now
            )
        return CockpitSettingsView(slug=slug, values=stored.values, updated_at=stored.updated_at)

    async def put_layout(
        self, principal: Principal, device_class: DeviceClass, request: CockpitLayoutWrite
    ) -> CockpitLayoutView:
        """Arrange one device class's board from live widgets at sizes their manifests offer."""

        require_scope(principal, "cockpit.write")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            for placement in request.placements:
                try:
                    _widget, live = await self._live(uow, principal, placement.slug)
                except NotFoundError:
                    raise CockpitValidationError(f"{placement.slug} is not a live widget") from None
                if placement.size.value not in (_manifest(live).get("sizes") or []):
                    raise CockpitValidationError(
                        f"{placement.slug} does not offer the {placement.size.value} size"
                    )
            try:
                layout = Layout(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    device_class=device_class,
                    placements=request.placements,
                    revision=(request.expected_revision or 0) + 1,
                    updated_at=now,
                )
            except ValueError as error:
                raise CockpitValidationError(f"the layout is invalid: {error}") from error
            stored = await uow.cockpit_widgets.put_layout(
                layout, expected_revision=request.expected_revision
            )
            await uow.cockpit_events.append(
                principal,
                kind=CockpitEventKind.LAYOUT_UPDATED,
                subject=device_class.value,
                occurred_at=now,
            )
        return CockpitLayoutView(revision=stored.revision, placements=stored.placements)

    async def revert(self, principal: Principal, slug: str, revision_id: UUID) -> CockpitChangeView:
        require_scope(principal, "cockpit.write")
        return self._change(
            await self._built(self._publication.revert(principal, slug, revision_id))
        )

    async def restore(self, principal: Principal, slug: str) -> CockpitChangeView:
        require_scope(principal, "cockpit.write")
        return self._change(await self._built(self._publication.restore(principal, slug)))

    async def delete(self, principal: Principal, slug: str) -> CockpitChangeView:
        require_scope(principal, "cockpit.write")
        entry = await self._publication.delete(principal, slug)
        return CockpitChangeView(slug=entry.slug, operation="delete", state="queued")

    @staticmethod
    async def _built(change: Any) -> WidgetRevision:
        try:
            revision: WidgetRevision = await change
        except WidgetBuildFailedError as error:
            raise CockpitBuildUnavailable(
                "the builder reached no verdict; nothing was recorded"
            ) from error
        return revision

    @staticmethod
    def _change(revision: WidgetRevision) -> CockpitChangeView:
        return CockpitChangeView(
            slug=revision.slug,
            operation="write",
            state=revision.state,
            revision_id=revision.id,
            diagnostics=revision.diagnostics,
        )

    async def stream(self, principal: Principal, after: int | None) -> AsyncIterator[StreamFrame]:
        """Replay events after `after` (from now when None) until the stream's lifetime."""

        require_scope(principal, "cockpit.read")
        deadline = self._clock.now() + self._stream_max
        if after is None:
            # The cursor is taken before the subscription opens: an event that
            # commits while it opens is then after the cursor and replayed.
            async with self._uow_factory() as uow:
                latest = await uow.cockpit_events.latest_sequence(principal)
            watermark = latest or 0
        else:
            watermark = after
        resuming = after is not None and after > 0
        async with self._live_events.listen(cockpit_channel(principal.tenant_id)) as subscription:
            while True:
                async with self._uow_factory() as uow:
                    events = await uow.cockpit_events.after(
                        principal, sequence=watermark, limit=_REPLAY_PAGE
                    )
                    # A resume point is checked against retention in the first
                    # page's unit of work, after the fetch: a sweep between the
                    # two can only cost a refetch, never hide a pruned gap.
                    oldest = (
                        await uow.cockpit_events.oldest_sequence(principal) if resuming else None
                    )
                if resuming:
                    resuming = False
                    if oldest is None or watermark < oldest:
                        # The client's last event was pruned, so events after it may be too.
                        yield TransientStreamFrame(event="stream.reset", data={})
                        return
                for event in events:
                    watermark = event.sequence
                    yield PersistedStreamFrame(
                        sequence=event.sequence,
                        event=event.kind.value,
                        data={"subject": event.subject},
                    )
                if len(events) == _REPLAY_PAGE:
                    continue
                remaining = (deadline - self._clock.now()).total_seconds()
                if remaining <= 0:
                    return
                await subscription.receive(min(_IDLE_POLL_SECONDS, remaining))


class CockpitRetention:
    """Keep stream events only for the resume window."""

    def __init__(
        self, *, uow_factory: UnitOfWorkFactory, clock: Clock, tenant_id: str, seconds: int
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._tenant_id = tenant_id
        self._window = timedelta(seconds=seconds)

    async def run_once(self) -> int:
        async with self._uow_factory() as uow:
            return await uow.cockpit_events.prune_before(
                self._tenant_id, before=self._clock.now() - self._window
            )
