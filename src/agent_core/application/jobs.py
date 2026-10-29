"""Saved jobs: private grants and commit-before-activate publication."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from agent_core.application.cockpit import screen_names, screen_settings_values
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    BuildDiagnostic,
    CommitOutboxEntry,
    ImportFailure,
    OutboxOperation,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.jobs import JobManifest, JobRecord, JobRevision
from agent_core.ports.cockpit import (
    BranchMovedError,
    BuildScreen,
    JsonSchemaCheck,
    WidgetRepository,
    WidgetRepositoryUnavailableError,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.jobs import JobScreen
from agent_core.ports.persistence import UnitOfWorkFactory


def job_commit_message(slug: str, revision_id: UUID, *, archive: bool = False) -> str:
    """Create a fixed publication message with the revision trailer used for reconciliation."""
    operation = "Archive" if archive else "Save"
    return f"Jobs: {operation} {slug}\n\nVeetbot-Revision: {revision_id}\n"


def _strings(value: object) -> list[str]:
    """Collect nested private input strings for the publication privacy screen."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


class JobPublication:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        schema_check: JsonSchemaCheck,
        screen: JobScreen,
        repository: WidgetRepository | None = None,
        owner_names: tuple[str, ...] = (),
    ) -> None:
        self.uow_factory = uow_factory
        self.clock = clock
        self.ids = ids
        self.schema_check = schema_check
        self.screen = screen
        self.repository = repository
        self.owner_names = owner_names

    async def write(
        self,
        *,
        principal: Principal,
        slug: str,
        source: str,
        manifest: JobManifest,
        expected_revision: UUID | None,
        request_key: str,
        archive: bool = False,
    ) -> JobRevision:
        """Screen source and atomically save a pending revision and publication request."""
        key = f"job.request:{principal.tenant_id}:{principal.principal_id}:{request_key}"
        async with self.uow_factory() as uow:
            replay = await uow.process_events.get_by_derivation(key)
            if replay is not None:
                saved = await uow.jobs.get(principal, str(replay.payload["slug"]))
                return saved.revisions[str(replay.payload["revision_id"])]
            try:
                saved = await uow.jobs.get(principal, slug)
            except NotFoundError:
                saved = JobRecord(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    slug=slug,
                    version=1,
                    updated_at=self.clock.now(),
                )
                version = None
            else:
                version = saved.version
            if saved.live_revision_id != expected_revision or any(
                item.state == "pending_commit" for item in saved.revisions.values()
            ):
                raise ConflictError(
                    "The job revision changed or publication is pending.", reason="job_changed"
                )
            names = await screen_names(uow, principal, self.owner_names)
            values = list(await screen_settings_values(uow, principal))
            for job in await uow.jobs.list(principal):
                for grant in job.grants.values():
                    values.extend(_strings(grant.access.inputs))
        if self.schema_check.schema_violations(manifest.input_schema):
            raise ValueError("Job input schema must be a self-contained JSON Schema.")
        revision_id = self.ids.new_id()
        revision = JobRevision(
            id=revision_id,
            source=source,
            manifest=manifest,
            state="pending_commit",
            created_at=self.clock.now(),
        )
        diagnostics = await self.screen.check(
            principal,
            revision.files(),
            screen=BuildScreen(
                names=names,
                settings_values=tuple(values),
                commit_message=job_commit_message(slug, revision_id, archive=archive),
            ),
        )
        if diagnostics:
            raise ValueError("; ".join(f"{item.rule}: {item.message}" for item in diagnostics))
        revisions = {**saved.revisions, str(revision_id): revision}
        previous = None if expected_revision is None else saved.revisions[str(expected_revision)]
        changed = saved.model_copy(
            update={
                "version": (version or 0) + 1,
                "revisions": revisions,
                "updated_at": self.clock.now(),
            }
        )
        async with self.uow_factory() as uow:
            await uow.jobs.put(changed, expected_version=version)
            await uow.job_outbox.enqueue(
                CommitOutboxEntry(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    revision_id=revision_id,
                    slug=slug,
                    operation=OutboxOperation.REMOVE if archive else OutboxOperation.WRITE,
                    next_attempt_at=self.clock.now(),
                    created_at=self.clock.now(),
                    expected_parent_sha=None if previous is None else previous.commit_sha,
                )
            )
            await uow.process_events.append(
                ProcessEvent(
                    id=self.ids.new_id(),
                    event_type="job.request",
                    actor_type="jobs",
                    actor_id=principal.principal_id,
                    derivation_key=key,
                    payload={
                        "tenant_id": principal.tenant_id,
                        "principal_id": principal.principal_id,
                        "slug": slug,
                        "revision_id": str(revision_id),
                    },
                    created_at=self.clock.now(),
                )
            )
        return revision

    async def flush(self, principal: Principal) -> int:
        """Commit due job changes, reconciling lost responses by their revision trailer."""
        if self.repository is None:
            return 0
        async with self.uow_factory() as uow:
            entries = await uow.job_outbox.due(principal.tenant_id, now=self.clock.now(), limit=100)
        completed = 0
        for entry in entries:
            if entry.principal_id != principal.principal_id:
                continue
            try:
                await self._commit(principal, entry)
            except (WidgetRepositoryUnavailableError, BranchMovedError, ConflictError):
                async with self.uow_factory() as uow:
                    await uow.job_outbox.reschedule(
                        principal,
                        entry.revision_id,
                        attempts=entry.attempts + 1,
                        next_attempt_at=self.clock.now() + timedelta(seconds=30),
                        last_error=(
                            "Job publication is waiting for the repository or a concurrent edit."
                        ),
                    )
            else:
                completed += 1
        return completed

    async def _commit(self, principal: Principal, entry: CommitOutboxEntry) -> None:
        """Publish or reconcile one queued revision while preserving intervening human edits."""
        repository = self.repository
        assert repository is not None
        async with self.uow_factory() as uow:
            saved = await uow.jobs.get(principal, entry.slug)
        revision = saved.revisions[str(entry.revision_id)]
        if revision.commit_sha is not None:
            async with self.uow_factory() as uow:
                await uow.job_outbox.complete(principal, entry.revision_id)
            return
        committed = await repository.find_revision(entry.revision_id, limit=200)
        archive = entry.operation is OutboxOperation.REMOVE
        if committed is None:
            head = await repository.ensure_branch()
            current_files = await repository.read_widget(entry.slug, at=head)
            expected_files = (
                None
                if entry.expected_parent_sha is None
                else await repository.read_widget(
                    entry.slug,
                    at=entry.expected_parent_sha,
                )
            )
            if current_files != expected_files:
                # A human's newer package is never overwritten by a queued edit.
                async with self.uow_factory() as uow:
                    revisions = {
                        **saved.revisions,
                        str(revision.id): revision.model_copy(update={"state": "conflicted"}),
                    }
                    await uow.jobs.put(
                        saved.model_copy(
                            update={"version": saved.version + 1, "revisions": revisions}
                        ),
                        expected_version=saved.version,
                    )
                    await uow.job_outbox.complete(principal, revision.id)
                return
            message = job_commit_message(entry.slug, entry.revision_id, archive=archive)
            committed = (
                await repository.remove_widget(entry.slug, parent_sha=head, message=message)
                if archive
                else await repository.write_widget(
                    entry.slug, revision.files(), parent_sha=head, message=message
                )
            )
        async with self.uow_factory() as uow:
            current = await uow.jobs.get(principal, entry.slug)
            revisions = dict(current.revisions)
            if current.live_revision_id is not None:
                old = revisions[str(current.live_revision_id)]
                revisions[str(old.id)] = old.model_copy(update={"state": "superseded"})
            revisions[str(revision.id)] = revision.model_copy(
                update={
                    "state": "archived" if archive else "live",
                    "commit_sha": committed,
                }
            )
            await uow.jobs.put(
                current.model_copy(
                    update={
                        "version": current.version + 1,
                        "revisions": revisions,
                        "live_revision_id": revision.id,
                        "archived": archive,
                        "updated_at": self.clock.now(),
                    }
                ),
                expected_version=current.version,
            )
            await uow.job_outbox.complete(principal, revision.id)

    async def import_changes(self, principal: Principal) -> int:
        """Import screened human edits without changing any existing execution pin."""
        if self.repository is None:
            return 0
        async with self.uow_factory() as uow:
            events = await uow.process_events.list_filtered(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                session_id=None,
                event_types=frozenset({"job.import"}),
                limit=1,
            )
        cursor = str(events[0].payload["commit_sha"]) if events else None
        commits = await self.repository.commits_since(cursor, limit=100)
        for commit in commits:
            key = f"job.import:{principal.tenant_id}:{principal.principal_id}:{commit.sha}"
            async with self.uow_factory() as uow:
                if await uow.process_events.get_by_derivation(key):
                    continue
                names = await screen_names(uow, principal, self.owner_names)
                values = list(await screen_settings_values(uow, principal))
                records = await uow.jobs.list(principal)
                for item in records:
                    for grant in item.grants.values():
                        values.extend(_strings(grant.access.inputs))
            for slug in commit.changed_slugs:
                saved = next((item for item in records if item.slug == slug), None)
                files = await self.repository.read_widget(slug, at=commit.sha)
                if saved is not None and any(
                    r.commit_sha == commit.sha for r in saved.revisions.values()
                ):
                    continue
                pending = (
                    None
                    if saved is None
                    else saved.revisions.get(commit.trailers.get("Veetbot-Revision", ""))
                )
                if (
                    pending is not None
                    and pending.state == "pending_commit"
                    and (files == pending.files() or files is None)
                ):
                    continue  # The outbox reconciles its own acknowledged or lost-response write.
                try:
                    if commit.outside_widgets:
                        raise ValueError("The commit changes paths outside cockpit/jobs/.")
                    revision = None
                    if files is not None:
                        if set(files) != {"job.json", "main.py"}:
                            raise ValueError("A job package contains only job.json and main.py.")
                        manifest = JobManifest.model_validate_json(files["job.json"])
                        if self.schema_check.schema_violations(manifest.input_schema):
                            raise ValueError("Invalid job input schema.")
                        revision = JobRevision(
                            id=self.ids.new_id(),
                            manifest=manifest,
                            source=files["main.py"],
                            state="live",
                            commit_sha=commit.sha,
                            created_at=self.clock.now(),
                        )
                        diagnostics = await self.screen.check(
                            principal,
                            files,
                            screen=BuildScreen(
                                names=names,
                                settings_values=tuple(values),
                                commit_message=commit.message,
                            ),
                        )
                        if diagnostics:
                            raise ValueError("Job privacy screen rejected the imported package.")
                    if saved is None:
                        if revision is None:
                            continue
                        saved = JobRecord(
                            tenant_id=principal.tenant_id,
                            principal_id=principal.principal_id,
                            slug=slug,
                            version=1,
                            updated_at=self.clock.now(),
                        )
                        expected = None
                    else:
                        expected = saved.version
                    revisions = dict(saved.revisions)
                    if saved.live_revision_id is not None:
                        old = revisions[str(saved.live_revision_id)]
                        revisions[str(old.id)] = old.model_copy(
                            update={"state": "archived" if revision is None else "superseded"}
                        )
                    if revision is not None:
                        revisions[str(revision.id)] = revision
                    async with self.uow_factory() as uow:
                        await uow.jobs.put(
                            saved.model_copy(
                                update={
                                    "version": (expected or 0) + 1,
                                    "revisions": revisions,
                                    "live_revision_id": revision.id
                                    if revision
                                    else saved.live_revision_id,
                                    "archived": revision is None,
                                    "updated_at": self.clock.now(),
                                }
                            ),
                            expected_version=expected,
                        )
                except ValueError:
                    async with self.uow_factory() as uow:
                        await uow.job_outbox.record_import_failure(
                            ImportFailure(
                                tenant_id=principal.tenant_id,
                                principal_id=principal.principal_id,
                                id=self.ids.new_id(),
                                commit_sha=commit.sha,
                                slug=slug,
                                diagnostics=(
                                    BuildDiagnostic(
                                        rule="job.import_rejected",
                                        message=(
                                            "The job package failed validation "
                                            "or privacy screening."
                                        ),
                                    ),
                                ),
                                created_at=self.clock.now(),
                            )
                        )
            async with self.uow_factory() as uow:
                await uow.process_events.append(
                    ProcessEvent(
                        id=self.ids.new_id(),
                        event_type="job.import",
                        actor_type="jobs",
                        actor_id=principal.principal_id,
                        derivation_key=key,
                        payload={
                            "tenant_id": principal.tenant_id,
                            "principal_id": principal.principal_id,
                            "commit_sha": commit.sha,
                        },
                        created_at=self.clock.now(),
                    )
                )
        return len(commits)
