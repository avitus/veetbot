"""Cockpit widget publication: build, commit, then activate (Milestone 35).

docs/plan/cockpit.md, the repository branch and the commit outbox. A widget
change is built and privacy-screened first; a rejection records the revision
with its diagnostics and nothing else. An accepted change is stored as
`pending_commit` with its outbox entry in one transaction. The maintenance
role's outbox worker commits it to the public branch and only then makes it
live, and the importer brings human commits through the same builder.

A commit belongs to this deployment when its `Veetbot-Revision` trailer names a
revision or recorded commit of the principal; anything else is a person's.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    BuildDiagnostic,
    CockpitEventKind,
    CommitOperation,
    CommitOutboxEntry,
    ImportFailure,
    OutboxOperation,
    RevisionSource,
    RevisionState,
    Widget,
    WidgetRevision,
    WidgetState,
    is_widget_slug,
    package_violation,
    widget_commit_message,
)
from agent_core.domain.errors import ConflictError, NotFoundError, RateLimitedError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleQuery, Person, PersonIdentifier
from agent_core.ports.cockpit import (
    BranchMovedError,
    BuildScreen,
    WidgetBuild,
    WidgetBuilder,
    WidgetPathRefusedError,
    WidgetRepository,
    WidgetRepositoryUnavailableError,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

REVISION_EVENT = "cockpit.revision"
COMMIT_EVENT = "cockpit.commit"
IMPORT_EVENT = "cockpit.import"
_HOUR = timedelta(hours=1)
# Every queued entry, so a later entry never overtakes an earlier one for its widget.
_QUEUE_HORIZON = timedelta(days=3650)
_PEOPLE_PAGE = 100

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CockpitPublicationLimits:
    """The knobs of `cockpit/profiles.yaml` the publication path reads."""

    commits_per_widget_per_hour: int = 12
    commits_per_principal_per_hour: int = 60
    history_depth: int = 200
    import_batch: int = 50
    branch_move_retries: int = 3
    retry_base_seconds: int = 15
    retry_max_seconds: int = 900


def _commit_key(principal: Principal, revision_id: UUID) -> str:
    return f"{COMMIT_EVENT}:{principal.tenant_id}:{principal.principal_id}:{revision_id}"


def _import_key(principal: Principal, commit_sha: str) -> str:
    return f"{IMPORT_EVENT}:{principal.tenant_id}:{principal.principal_id}:{commit_sha}"


def _owner(principal: Principal) -> dict[str, str]:
    return {"tenant_id": principal.tenant_id, "principal_id": principal.principal_id}


def _storable(files: Mapping[str, str]) -> dict[str, str]:
    """The package a revision may keep; a rejected package outside the limits keeps none."""

    return {} if package_violation(files) is not None else dict(files)


def _operation(
    source: RevisionSource, expected_parent_sha: str | None, state: WidgetState
) -> CommitOperation:
    """The commit message's verb, from what neither acceptance nor the commit can change.

    The screen checks the exact message the outbox writes, so both derive it here.
    """

    if source is RevisionSource.REVERT:
        return "restore" if state is WidgetState.DELETED else "revert"
    return "update" if expected_parent_sha is not None else "create"


class _PersonWonError(Exception):
    """A person changed the widget on the branch after the change was based on it."""


class _Events:
    """The ordinary event log entries publication writes; payloads carry no content."""

    def __init__(self, clock: Clock, ids: IdFactory) -> None:
        self._clock = clock
        self._ids = ids

    async def record(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        event_type: str,
        *,
        key: str | None = None,
        **payload: Any,
    ) -> None:
        event_id = self._ids.new_id()
        await uow.process_events.append(
            ProcessEvent(
                id=event_id,
                event_type=event_type,
                actor_type="cockpit",
                actor_id=principal.principal_id,
                payload={**_owner(principal), **payload},
                derivation_key=key or f"{event_type}:{event_id}",
                created_at=self._clock.now(),
            )
        )


async def screen_names(
    uow: RepositoryUnitOfWork, principal: Principal, owner_names: Sequence[str]
) -> tuple[str, ...]:
    """Every name the privacy screen must keep off the public branch."""

    names: set[str] = {name for name in owner_names if name.strip()}
    for kind in ("person", "identifier"):
        query = PeopleQuery(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            kinds=[kind],
            sensitivity_ceiling=Sensitivity.RESTRICTED,
            include_superseded=True,
            limit=_PEOPLE_PAGE,
        )
        while True:
            page = await uow.people.query(query)
            for row in page[:_PEOPLE_PAGE]:
                if isinstance(row, Person):
                    names.add(row.display_name)
                elif isinstance(row, PersonIdentifier) and row.identifier_kind == "name":
                    names.add(row.value)
            if len(page) <= _PEOPLE_PAGE:
                break
            query = query.model_copy(update={"after": page[_PEOPLE_PAGE - 1].id})
    return tuple(sorted(names))


async def screen_settings_values(
    uow: RepositoryUnitOfWork, principal: Principal
) -> tuple[str, ...]:
    """Every current private settings string, across all of the principal's widgets."""

    values: set[str] = set()
    for widget in await uow.cockpit_widgets.list_widgets(principal, include_deleted=True):
        settings = await uow.cockpit_widgets.get_settings(principal, widget.slug)
        if settings is not None:
            values.update(value for value in settings.values.values() if isinstance(value, str))
    return tuple(sorted(values))


async def import_cursor(uow: RepositoryUnitOfWork, principal: Principal) -> str | None:
    """The last branch commit the importer recorded; every commit after it is unimported."""

    latest = await uow.process_events.list_filtered(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        session_id=None,
        event_types=frozenset({IMPORT_EVENT}),
        limit=1,
    )
    return str(latest[0].payload["commit_sha"]) if latest else None


async def issued_by_platform(
    uow: RepositoryUnitOfWork, principal: Principal, trailer: str | None
) -> bool:
    """Whether a commit's `Veetbot-Revision` trailer names something this deployment wrote."""

    if trailer is None:
        return False
    try:
        revision_id = UUID(trailer)
    except ValueError:
        return False
    if await uow.process_events.get_by_derivation(_commit_key(principal, revision_id)):
        return True
    try:
        revision = await uow.cockpit_widgets.get_revision(principal, revision_id)
    except NotFoundError:
        return False
    return revision.source is not RevisionSource.OWNER_GIT


class CockpitPublication:
    """Accept widget changes: build and screen them, then queue their commit."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        builder: WidgetBuilder,
        owner_names: Sequence[str] = (),
        limits: CockpitPublicationLimits | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._builder = builder
        self._owner_names = tuple(owner_names)
        self._limits = limits or CockpitPublicationLimits()
        self._events = _Events(clock, ids)

    async def _widget(
        self, uow: RepositoryUnitOfWork, principal: Principal, slug: str
    ) -> Widget | None:
        try:
            return await uow.cockpit_widgets.get_widget(principal, slug)
        except NotFoundError:
            return None

    @staticmethod
    async def _head(
        uow: RepositoryUnitOfWork, principal: Principal, widget: Widget | None
    ) -> UUID | None:
        """The newest accepted revision: one waiting for its commit, or else the live one.

        An update names it as `expected_revision`, so a change accepted but not yet
        committed is never silently overwritten. A deleted widget has none.
        """

        if widget is None or widget.state is WidgetState.DELETED:
            return None
        recent = await uow.cockpit_widgets.list_revisions(principal, widget.slug, limit=50)
        waiting = (RevisionState.PENDING_COMMIT, RevisionState.COMMITTED)
        newest = next((revision for revision in recent if revision.state in waiting), None)
        return newest.id if newest is not None else widget.live_revision_id

    async def _check_rate(self, uow: RepositoryUnitOfWork, principal: Principal, slug: str) -> None:
        since = self._clock.now() - _HOUR
        limits = self._limits
        widgets = await uow.cockpit_widgets.list_widgets(principal, include_deleted=True)
        total = 0
        for widget in widgets:
            recent = await uow.cockpit_widgets.list_revisions(
                principal, widget.slug, limit=limits.commits_per_principal_per_hour + 1
            )
            counted = sum(
                1
                for revision in recent
                if revision.created_at > since and revision.state is not RevisionState.REJECTED
            )
            if widget.slug == slug and counted >= limits.commits_per_widget_per_hour:
                raise RateLimitedError(
                    "cockpit_widget_rate", f"{slug} reached its commits for this hour"
                )
            total += counted
        if total >= limits.commits_per_principal_per_hour:
            raise RateLimitedError("cockpit_principal_rate", "the hour's widget commits are used")

    async def _screen(self, principal: Principal, message: str) -> BuildScreen:
        async with self._uow_factory() as uow:
            return BuildScreen(
                names=await screen_names(uow, principal, self._owner_names),
                settings_values=await screen_settings_values(uow, principal),
                commit_message=message,
            )

    async def _accept(
        self,
        principal: Principal,
        slug: str,
        files: Mapping[str, str],
        *,
        operation: CommitOperation,
        source: RevisionSource,
        expected_head: UUID | None,
        expected_parent_sha: str | None,
        origin_run_id: UUID | None,
    ) -> WidgetRevision:
        revision_id = self._ids.new_id()
        message = widget_commit_message(operation, slug, revision_id)
        build = await self._builder.build(
            principal, slug, files, screen=await self._screen(principal, message)
        )
        now = self._clock.now()
        async with self._uow_factory() as uow:
            widget = await self._widget(uow, principal, slug)
            if widget is None:
                widget = await uow.cockpit_widgets.create_widget(
                    Widget(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        slug=slug,
                        state=WidgetState.ACTIVE,
                        created_at=now,
                        updated_at=now,
                    )
                )
            elif await self._head(uow, principal, widget) != expected_head:
                raise ConflictError(f"{slug} changed while it was built", reason="revision_changed")
            revision = await uow.cockpit_widgets.add_revision(
                self._revision(
                    principal, revision_id, slug, files, build, source, origin_run_id, now
                )
            )
            await self._events.record(
                uow,
                principal,
                REVISION_EVENT,
                revision_id=str(revision_id),
                slug=slug,
                state=revision.state.value,
                rules=sorted({diagnostic.rule for diagnostic in revision.diagnostics}),
            )
            if revision.state is RevisionState.PENDING_COMMIT:
                await uow.cockpit_outbox.enqueue(
                    CommitOutboxEntry(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        revision_id=revision_id,
                        slug=slug,
                        operation=OutboxOperation.WRITE,
                        next_attempt_at=now,
                        expected_parent_sha=expected_parent_sha,
                        created_at=now,
                    )
                )
            return revision

    @staticmethod
    def _revision(
        principal: Principal,
        revision_id: UUID,
        slug: str,
        files: Mapping[str, str],
        build: WidgetBuild,
        source: RevisionSource,
        origin_run_id: UUID | None,
        now: datetime,
    ) -> WidgetRevision:
        common: dict[str, Any] = {
            **_owner(principal),
            "id": revision_id,
            "slug": slug,
            "source": source,
            "origin_run_id": origin_run_id,
            "created_at": now,
            "updated_at": now,
        }
        if not build.ok:
            return WidgetRevision(
                **common,
                state=RevisionState.REJECTED,
                files=_storable(files),
                diagnostics=build.diagnostics,
            )
        return WidgetRevision(
            **common,
            state=RevisionState.PENDING_COMMIT,
            files=dict(files),
            bundle=build.bundle,
            bundle_sha256=build.bundle_sha256,
            kit_api=build.kit_api,
        )

    async def _live_commit(
        self, uow: RepositoryUnitOfWork, principal: Principal, widget: Widget | None
    ) -> str | None:
        if widget is None or widget.live_revision_id is None:
            return None
        live = await uow.cockpit_widgets.get_revision(principal, widget.live_revision_id)
        return live.commit_sha

    async def write(
        self,
        principal: Principal,
        slug: str,
        files: Mapping[str, str],
        *,
        expected_revision: UUID | None,
        origin_run_id: UUID | None = None,
    ) -> WidgetRevision:
        """Create a widget (`expected_revision` None) or update its newest revision."""

        if not is_widget_slug(slug):
            raise ValueError("a widget slug follows the manifest grammar")
        async with self._uow_factory() as uow:
            widget = await self._widget(uow, principal, slug)
            head = await self._head(uow, principal, widget)
            if expected_revision != head:
                raise ConflictError(f"{slug} is at another revision", reason="revision_changed")
            await self._check_rate(uow, principal, slug)
            parent = await self._live_commit(uow, principal, widget) if head else None
        return await self._accept(
            principal,
            slug,
            files,
            operation=_operation(RevisionSource.ASSISTANT, parent, WidgetState.ACTIVE),
            source=RevisionSource.ASSISTANT,
            expected_head=head,
            expected_parent_sha=parent,
            origin_run_id=origin_run_id,
        )

    async def _republish(
        self, principal: Principal, widget: Widget, revision_id: UUID
    ) -> WidgetRevision:
        slug = widget.slug
        async with self._uow_factory() as uow:
            target = await uow.cockpit_widgets.get_revision(principal, revision_id)
            if target.slug != slug or not target.files or target.state is RevisionState.REJECTED:
                raise NotFoundError(f"widget revision {revision_id} not found")
            await self._check_rate(uow, principal, slug)
            parent = await self._live_commit(uow, principal, widget)
            head = await self._head(uow, principal, widget)
        return await self._accept(
            principal,
            slug,
            target.files,
            operation=_operation(RevisionSource.REVERT, parent, widget.state),
            source=RevisionSource.REVERT,
            expected_head=head,
            expected_parent_sha=parent,
            origin_run_id=None,
        )

    async def revert(self, principal: Principal, slug: str, revision_id: UUID) -> WidgetRevision:
        """A new revision with an earlier revision's files, through the whole pipeline."""

        async with self._uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(principal, slug)
        if widget.state is WidgetState.DELETED:
            raise ConflictError(f"{slug} is deleted; restore it first", reason="widget_deleted")
        return await self._republish(principal, widget, revision_id)

    async def restore(self, principal: Principal, slug: str) -> WidgetRevision:
        """Commit a deleted widget's last live package again and reactivate it."""

        async with self._uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(principal, slug)
        if widget.state is not WidgetState.DELETED or widget.live_revision_id is None:
            raise ConflictError(f"{slug} is not deleted", reason="widget_not_deleted")
        return await self._republish(principal, widget, widget.live_revision_id)

    async def delete(self, principal: Principal, slug: str) -> CommitOutboxEntry:
        """Queue the directory's removal; the widget is deleted once it is committed."""

        now = self._clock.now()
        async with self._uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(principal, slug)
            if widget.state is WidgetState.DELETED:
                raise ConflictError(f"{slug} is already deleted", reason="widget_deleted")
            operation_id = self._ids.new_id()
            return await uow.cockpit_outbox.enqueue(
                CommitOutboxEntry(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    revision_id=operation_id,
                    slug=slug,
                    operation=OutboxOperation.REMOVE,
                    next_attempt_at=now,
                    expected_parent_sha=await self._live_commit(uow, principal, widget),
                    created_at=now,
                )
            )


class CommitOutboxWorker:
    """Commit queued revisions in order per widget, then make them live."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        repository: WidgetRepository,
        principal: Principal,
        limits: CockpitPublicationLimits | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._repository = repository
        self._principal = principal
        self._limits = limits or CockpitPublicationLimits()
        self._events = _Events(clock, ids)

    async def run_once(self) -> int:
        """Attempt every due entry once; return how many reached an outcome."""

        now = self._clock.now()
        async with self._uow_factory() as uow:
            queued = await uow.cockpit_outbox.due(
                self._principal.tenant_id, now=now + _QUEUE_HORIZON, limit=1000
            )
        blocked: set[str] = set()
        settled = 0
        for entry in queued:
            if entry.principal_id != self._principal.principal_id or entry.slug in blocked:
                continue
            if entry.next_attempt_at > now:
                blocked.add(entry.slug)
                continue
            try:
                settled += await self._attempt(entry)
            except WidgetRepositoryUnavailableError as error:
                await self._reschedule(entry, str(error))
                blocked.add(entry.slug)
            except Exception as error:
                # A refused path, a lost revision or an unexpected GitHub answer
                # is this entry's problem: it retries with backoff behind the
                # same widget, and every other widget's entry still commits.
                logger.exception(
                    "cockpit_outbox_entry_failed",
                    extra={"revision_id": str(entry.revision_id), "slug": entry.slug},
                )
                await self._reschedule(entry, f"{type(error).__name__}: {error}")
                blocked.add(entry.slug)
        return settled

    async def _reschedule(self, entry: CommitOutboxEntry, error: str) -> None:
        attempts = entry.attempts + 1
        delay = min(
            self._limits.retry_max_seconds,
            self._limits.retry_base_seconds * 2 ** min(entry.attempts, 16),
        )
        async with self._uow_factory() as uow:
            await uow.cockpit_outbox.reschedule(
                self._principal,
                entry.revision_id,
                attempts=attempts,
                next_attempt_at=self._clock.now() + timedelta(seconds=delay),
                last_error=error[:500],
            )
            await self._events.record(
                uow,
                self._principal,
                COMMIT_EVENT,
                revision_id=str(entry.revision_id),
                slug=entry.slug,
                outcome="retry",
                attempts=attempts,
            )

    async def _human_change(self, entry: CommitOutboxEntry) -> bool:
        """Whether a person changed this widget after the change was based on it.

        A person's version may already be imported and live, or still only on
        the branch after the change's base: the live commit it changed, or for a
        new widget the importer's cursor, after which every commit is unimported.
        """

        principal = self._principal
        async with self._uow_factory() as uow:
            try:
                widget = await uow.cockpit_widgets.get_widget(principal, entry.slug)
            except NotFoundError:
                widget = None
            if widget is not None and widget.live_revision_id is not None:
                live = await uow.cockpit_widgets.get_revision(principal, widget.live_revision_id)
                if (
                    live.source is RevisionSource.OWNER_GIT
                    and live.commit_sha != entry.expected_parent_sha
                    and live.created_at >= entry.created_at
                ):
                    return True
            baseline = entry.expected_parent_sha or await import_cursor(uow, principal)
        commits = await self._repository.commits_since(baseline, limit=self._limits.history_depth)
        async with self._uow_factory() as uow:
            for commit in commits:
                if entry.slug not in commit.changed_slugs:
                    continue
                trailer = commit.trailers.get("Veetbot-Revision")
                if trailer == str(entry.revision_id):
                    continue
                if not await issued_by_platform(uow, principal, trailer):
                    return True
        return False

    async def _attempt(self, entry: CommitOutboxEntry) -> int:
        principal = self._principal
        revision: WidgetRevision | None = None
        if entry.operation is OutboxOperation.WRITE:
            async with self._uow_factory() as uow:
                revision = await uow.cockpit_widgets.get_revision(principal, entry.revision_id)
            if revision.state is not RevisionState.PENDING_COMMIT:
                async with self._uow_factory() as uow:
                    await uow.cockpit_outbox.complete(principal, entry.revision_id)
                return 1
        # A retry after a lost response finds its own commit instead of writing twice.
        landed = await self._repository.find_revision(
            entry.revision_id, limit=self._limits.history_depth
        )
        if landed is None:
            try:
                if await self._human_change(entry):
                    raise _PersonWonError
                landed = await self._commit(entry, revision)
            except _PersonWonError:
                await self._settle(entry, revision, None, conflicted=True)
                return 1
        await self._settle(entry, revision, landed, conflicted=False)
        return 1

    async def _commit(
        self, entry: CommitOutboxEntry, revision: WidgetRevision | None
    ) -> str | None:
        operation: CommitOperation = "delete"
        if revision is not None:
            async with self._uow_factory() as uow:
                widget = await uow.cockpit_widgets.get_widget(self._principal, entry.slug)
            operation = _operation(revision.source, entry.expected_parent_sha, widget.state)
        message = widget_commit_message(operation, entry.slug, entry.revision_id)
        for _ in range(self._limits.branch_move_retries + 1):
            head = await self._repository.head() or await self._repository.ensure_branch()
            try:
                if revision is None:
                    return await self._repository.remove_widget(
                        entry.slug, parent_sha=head, message=message
                    )
                return await self._repository.write_widget(
                    entry.slug, revision.files, parent_sha=head, message=message
                )
            except BranchMovedError:
                if await self._human_change(entry):
                    raise _PersonWonError from None
                continue
            except WidgetPathRefusedError:
                if revision is None:
                    return None  # The directory is already gone from the branch.
                raise
        raise WidgetRepositoryUnavailableError("the widget branch kept moving")

    async def _settle(
        self,
        entry: CommitOutboxEntry,
        revision: WidgetRevision | None,
        commit_sha: str | None,
        *,
        conflicted: bool,
    ) -> None:
        principal = self._principal
        now = self._clock.now()
        async with self._uow_factory() as uow:
            outcome: str
            if conflicted and revision is not None:
                await uow.cockpit_widgets.set_revision_state(
                    principal,
                    entry.revision_id,
                    expected=RevisionState.PENDING_COMMIT,
                    state=RevisionState.CONFLICTED,
                    updated_at=now,
                )
                outcome = "conflicted"
            elif revision is None:
                if not conflicted:
                    await uow.cockpit_widgets.mark_deleted(principal, entry.slug, updated_at=now)
                    await uow.cockpit_events.append(
                        principal,
                        kind=CockpitEventKind.WIDGET_DELETED,
                        subject=entry.slug,
                        occurred_at=now,
                    )
                outcome = "conflicted" if conflicted else "deleted"
            else:
                await uow.cockpit_widgets.set_revision_state(
                    principal,
                    entry.revision_id,
                    expected=RevisionState.PENDING_COMMIT,
                    state=RevisionState.COMMITTED,
                    updated_at=now,
                    commit_sha=commit_sha,
                )
                await uow.cockpit_widgets.activate(
                    principal, entry.slug, entry.revision_id, updated_at=now
                )
                await uow.cockpit_events.append(
                    principal,
                    kind=CockpitEventKind.WIDGET_REVISED,
                    subject=entry.slug,
                    occurred_at=now,
                )
                outcome = "live"
            await uow.cockpit_outbox.complete(principal, entry.revision_id)
            await self._events.record(
                uow,
                principal,
                COMMIT_EVENT,
                key=None if conflicted else _commit_key(principal, entry.revision_id),
                revision_id=str(entry.revision_id),
                slug=entry.slug,
                outcome=outcome,
                commit_sha=commit_sha,
            )


class BranchImporter:
    """Bring human commits on the widget branch through the builder."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        repository: WidgetRepository,
        builder: WidgetBuilder,
        principal: Principal,
        owner_names: Sequence[str] = (),
        limits: CockpitPublicationLimits | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._repository = repository
        self._builder = builder
        self._principal = principal
        self._owner_names = tuple(owner_names)
        self._limits = limits or CockpitPublicationLimits()
        self._events = _Events(clock, ids)

    async def _cursor(self) -> str | None:
        """The last commit an import recorded; each import records its commit atomically."""

        async with self._uow_factory() as uow:
            return await import_cursor(uow, self._principal)

    async def run_once(self) -> int:
        """Import the commits after the cursor; return how many were processed."""

        head = await self._repository.head()
        cursor = await self._cursor()
        if head is None or head == cursor:
            return 0
        commits = await self._repository.commits_since(cursor, limit=self._limits.import_batch)
        for commit in commits:
            await self._import(
                commit.sha,
                commit.trailers.get("Veetbot-Revision"),
                commit.changed_slugs,
                commit.outside_widgets,
            )
        return len(commits)

    async def _import(
        self,
        commit_sha: str,
        trailer: str | None,
        changed_slugs: Sequence[str],
        outside_widgets: bool,
    ) -> None:
        principal = self._principal
        async with self._uow_factory() as uow:
            if await uow.process_events.get_by_derivation(_import_key(principal, commit_sha)):
                return  # Already imported; a rewound cursor never imports twice.
            ours = await issued_by_platform(uow, principal, trailer)
            names = await screen_names(uow, principal, self._owner_names)
            values = await screen_settings_values(uow, principal)
        builds: dict[str, tuple[dict[str, str], WidgetBuild] | None] = {}
        if not ours:
            for slug in changed_slugs:
                files = await self._repository.read_widget(slug, at=commit_sha)
                if files is None:
                    builds[slug] = None
                    continue
                screen = BuildScreen(names=names, settings_values=values)
                builds[slug] = (
                    files,
                    await self._builder.build(principal, slug, files, screen=screen),
                )
        now = self._clock.now()
        async with self._uow_factory() as uow:
            failed: builtins.list[str | None] = []
            if not ours and outside_widgets:
                await self._fail(
                    uow,
                    commit_sha,
                    None,
                    (
                        BuildDiagnostic(
                            rule="import.out_of_scope",
                            message="the commit changed paths outside cockpit/widgets/",
                        ),
                    ),
                )
                failed.append(None)
            for slug, built in builds.items():
                if built is None:
                    await self._delete(uow, slug, now)
                    continue
                files, build = built
                if not build.ok:
                    await self._fail(uow, commit_sha, slug, build.diagnostics)
                    failed.append(slug)
                    continue
                await self._activate(uow, slug, files, build, commit_sha, now)
            await self._events.record(
                uow,
                principal,
                IMPORT_EVENT,
                key=_import_key(principal, commit_sha),
                commit_sha=commit_sha,
                ours=ours,
                imported=sorted(
                    slug for slug, built in builds.items() if built is not None and built[1].ok
                ),
                failed=sorted(slug or "" for slug in failed),
            )

    async def _fail(
        self,
        uow: RepositoryUnitOfWork,
        commit_sha: str,
        slug: str | None,
        diagnostics: Sequence[BuildDiagnostic],
    ) -> None:
        now = self._clock.now()
        await uow.cockpit_outbox.record_import_failure(
            ImportFailure(
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                id=self._ids.new_id(),
                commit_sha=commit_sha,
                slug=slug,
                diagnostics=tuple(diagnostics),
                created_at=now,
            )
        )
        await uow.cockpit_events.append(
            self._principal,
            kind=CockpitEventKind.IMPORT_FAILED,
            subject=slug or "branch",
            occurred_at=now,
        )

    async def _delete(self, uow: RepositoryUnitOfWork, slug: str, now: datetime) -> None:
        try:
            widget = await uow.cockpit_widgets.get_widget(self._principal, slug)
        except NotFoundError:
            return
        if widget.state is WidgetState.ACTIVE:
            await uow.cockpit_widgets.mark_deleted(self._principal, slug, updated_at=now)
            await uow.cockpit_events.append(
                self._principal, kind=CockpitEventKind.WIDGET_DELETED, subject=slug, occurred_at=now
            )

    async def _activate(
        self,
        uow: RepositoryUnitOfWork,
        slug: str,
        files: dict[str, str],
        build: WidgetBuild,
        commit_sha: str,
        now: datetime,
    ) -> None:
        principal = self._principal
        try:
            await uow.cockpit_widgets.get_widget(principal, slug)
        except NotFoundError:
            await uow.cockpit_widgets.create_widget(
                Widget(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    slug=slug,
                    state=WidgetState.ACTIVE,
                    created_at=now,
                    updated_at=now,
                )
            )
        if package_violation(files) is not None:
            await self._fail(
                uow,
                commit_sha,
                slug,
                (BuildDiagnostic(rule="import.package", message="the package exceeds its limits"),),
            )
            return
        revision_id = self._ids.new_id()
        revision = WidgetRevision(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            id=revision_id,
            slug=slug,
            source=RevisionSource.OWNER_GIT,
            state=RevisionState.COMMITTED,
            files=files,
            bundle=build.bundle,
            bundle_sha256=build.bundle_sha256,
            kit_api=build.kit_api,
            commit_sha=commit_sha,
            created_at=now,
            updated_at=now,
        )
        await uow.cockpit_widgets.add_revision(revision)
        await uow.cockpit_widgets.activate(principal, slug, revision_id, updated_at=now)
        await uow.cockpit_events.append(
            principal, kind=CockpitEventKind.WIDGET_REVISED, subject=slug, occurred_at=now
        )
