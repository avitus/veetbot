"""Saved jobs run pinned local code, and activate only after public publication."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, cast

import pytest

from agent_core.adapters.cockpit.git import FakeWidgetRepository
from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.application.jobs import JobPublication
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import BuildDiagnostic
from agent_core.domain.jobs import JobManifest
from agent_core.ports.cockpit import BuildScreen, WidgetRepositoryUnavailableError
from tests.contract.support import memory_uow_factory, principal


class Screen:
    def __init__(self) -> None:
        self.reject = False
        self.seen: list[BuildScreen] = []

    async def check(
        self, principal: Principal, files: Mapping[str, str], *, screen: BuildScreen
    ) -> tuple[BuildDiagnostic, ...]:
        self.seen.append(screen)
        return (
            (BuildDiagnostic(rule="screen.name", message="Contains a known name."),)
            if self.reject
            else ()
        )


async def setup() -> tuple[Any, JobPublication, FakeWidgetRepository, Screen]:
    clock, factory = await memory_uow_factory()
    repository = FakeWidgetRepository(package_kind="job")
    screen = Screen()
    service = JobPublication(
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(),
        schema_check=Draft202012SchemaCheck(),
        screen=screen,
        repository=repository,
    )
    return factory, service, repository, screen


async def test_save_commits_then_activates_and_idempotent_retry_keeps_the_same_revision() -> None:
    factory, service, repository, screen = await setup()
    owner = principal()
    arguments: dict[str, Any] = {
        "principal": owner,
        "slug": "count-items",
        "source": "def main(inputs, job): return 3",
        "manifest": JobManifest(title="Count items"),
        "expected_revision": None,
        "request_key": "save-one",
    }
    revision = await service.write(**arguments)
    async with factory() as uow:
        saved = await uow.jobs.get(owner, "count-items")
    assert saved.live_revision_id is None
    assert revision.state == "pending_commit"
    assert await repository.head() is None
    assert await service.flush(owner) == 1
    async with factory() as uow:
        saved = await uow.jobs.get(owner, "count-items")
    assert saved.live_revision_id == revision.id
    live = saved.revisions[str(revision.id)]
    assert live.commit_sha is not None
    assert await repository.read_widget("count-items", at=live.commit_sha) == live.files()
    assert (await service.write(**arguments)).id == revision.id
    assert await service.flush(owner) == 0
    assert screen.seen[0].commit_message is not None


async def test_publication_outage_keeps_pending_source_and_retries_without_reauthoring() -> None:
    factory, service, repository, _ = await setup()
    owner = principal()
    revision = await service.write(
        principal=owner,
        slug="count-items",
        source="def main(inputs, job): return 3",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="save",
    )
    original = repository.ensure_branch

    async def unavailable() -> str:
        raise WidgetRepositoryUnavailableError("Offline")

    repository.ensure_branch = unavailable  # type: ignore[method-assign]
    assert await service.flush(owner) == 0
    async with factory() as uow:
        saved = await uow.jobs.get(owner, "count-items")
    assert saved.revisions[str(revision.id)].source == revision.source
    assert saved.live_revision_id is None
    repository.ensure_branch = original  # type: ignore[method-assign]
    cast(FixedClock, service.clock).advance(timedelta(minutes=1))
    assert await service.flush(owner) == 1


async def test_privacy_rejection_never_enqueues_publication() -> None:
    factory, service, repository, screen = await setup()
    screen.reject = True
    with pytest.raises(ValueError, match=r"screen\.name"):
        await service.write(
            principal=principal(),
            slug="count-items",
            source="def main(inputs, job): return 3",
            manifest=JobManifest(title="Count items"),
            expected_revision=None,
            request_key="save",
        )
    assert await service.flush(principal()) == 0
    assert await repository.head() is None
    async with factory() as uow:
        assert await uow.jobs.list(principal()) == []


async def test_human_commit_imports_screened_source_and_preserves_previous_pins() -> None:
    factory, service, repository, screen = await setup()
    owner = principal()
    original = await service.write(
        principal=owner,
        slug="count-items",
        source="def main(inputs, job): return 1",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="save",
    )
    await service.flush(owner)
    head = await repository.head()
    assert head is not None
    edited = {**original.files(), "main.py": "def main(inputs, job): return 2"}
    sha = await repository.write_widget(
        "count-items", edited, parent_sha=head, message="Improve the counter"
    )
    assert await service.import_changes(owner) >= 1
    async with factory() as uow:
        saved = await uow.jobs.get(owner, "count-items")
    assert saved.live_revision_id != original.id
    assert saved.revisions[str(saved.live_revision_id)].commit_sha == sha
    assert saved.revisions[str(original.id)].source == original.source
    assert await service.import_changes(owner) == 0
    screen.reject = True
    await repository.write_widget(
        "count-items",
        {**edited, "main.py": "def main(inputs, job): return 3"},
        parent_sha=sha,
        message="Rejected edit",
    )
    await service.import_changes(owner)
    async with factory() as uow:
        unchanged = await uow.jobs.get(owner, "count-items")
    assert unchanged.live_revision_id == saved.live_revision_id


async def test_archive_restore_and_lost_commit_response_keep_immutable_revisions() -> None:
    factory, service, repository, _ = await setup()
    owner = principal()
    first = await service.write(
        principal=owner,
        slug="count-items",
        source="def main(inputs, job): return 1",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="first",
    )
    write = repository.write_widget

    async def lose_response(*args: Any, **kwargs: Any) -> str:
        await write(*args, **kwargs)
        raise WidgetRepositoryUnavailableError("Lost response")

    repository.write_widget = lose_response  # type: ignore[method-assign]
    assert await service.flush(owner) == 0
    head = await repository.head()
    repository.write_widget = write  # type: ignore[method-assign]
    cast(FixedClock, service.clock).advance(timedelta(minutes=1))
    assert await service.flush(owner) == 1
    assert await repository.head() == head
    archived = await service.write(
        principal=owner,
        slug="count-items",
        source=first.source,
        manifest=first.manifest,
        expected_revision=first.id,
        request_key="archive",
        archive=True,
    )
    await service.flush(owner)
    async with factory() as uow:
        saved = await uow.jobs.get(owner, "count-items")
    assert saved.archived
    restored = await service.write(
        principal=owner,
        slug="count-items",
        source=first.source,
        manifest=first.manifest,
        expected_revision=archived.id,
        request_key="restore",
    )
    await service.flush(owner)
    async with factory() as uow:
        saved = await uow.jobs.get(owner, "count-items")
    assert not saved.archived and saved.live_revision_id == restored.id
    assert saved.revisions[str(first.id)].source == first.source
    assert saved.revisions[str(first.id)].commit_sha == head


def test_public_manifest_bytes_are_identical_across_process_hash_seeds() -> None:
    import json
    import os
    import subprocess
    import sys

    program = """
from agent_core.domain.jobs import JobManifest
manifest = JobManifest(
    title='Collector',
    calls=frozenset({'http.request', 'feed.publish', 'log', 'model.complete'}),
    feeds=frozenset({'agent.first', 'agent.second', 'agent.third'}),
)
assert isinstance(manifest.calls, frozenset) and isinstance(manifest.feeds, frozenset)
print(manifest.model_dump_json(indent=2))
"""
    outputs = [
        subprocess.run(
            [sys.executable, "-c", program],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        for seed in (0, 1, 2)
    ]
    assert len(set(outputs)) == 1
    manifest = json.loads(outputs[0])
    assert manifest["calls"] == sorted(manifest["calls"])
    assert manifest["feeds"] == sorted(manifest["feeds"])


@pytest.mark.parametrize("later_change", ["delete", "edit", "same_files"])
@pytest.mark.parametrize("existing", [False, True])
async def test_lost_publication_receipt_preserves_newer_imported_decisions(
    later_change: str,
    existing: bool,
) -> None:
    factory, service, repository, _ = await setup()
    owner = principal()
    previous = None
    if existing:
        previous = await service.write(
            principal=owner,
            slug="count-items",
            source="def main(inputs, job): return 0",
            manifest=JobManifest(title="Count items"),
            expected_revision=None,
            request_key="first",
        )
        await service.flush(owner)
    pending = await service.write(
        principal=owner,
        slug="count-items",
        source="def main(inputs, job): return 1",
        manifest=JobManifest(title="Count items"),
        expected_revision=None if previous is None else previous.id,
        request_key="pending",
    )
    write = repository.write_widget

    async def lose_response(*args: Any, **kwargs: Any) -> str:
        await write(*args, **kwargs)
        raise WidgetRepositoryUnavailableError("Lost response")

    repository.write_widget = lose_response  # type: ignore[method-assign]
    assert await service.flush(owner) == 0
    committed = await repository.head()
    assert committed is not None
    repository.write_widget = write  # type: ignore[method-assign]
    if later_change == "delete":
        await repository.remove_widget("count-items", parent_sha=committed, message="Stop job")
    else:
        files = pending.files()
        if later_change == "edit":
            files = {**files, "main.py": "def main(inputs, job): return 2"}
        await write("count-items", files, parent_sha=committed, message="Owner update")
    await service.import_changes(owner)
    async with factory() as uow:
        imported = await uow.jobs.get(owner, "count-items")
    assert imported.archived == (later_change == "delete")
    head = await repository.head()
    cast(FixedClock, service.clock).advance(timedelta(minutes=1))
    assert await service.flush(owner) == 1
    assert await service.flush(owner) == 0
    async with factory() as uow:
        reconciled = await uow.jobs.get(owner, "count-items")
    assert reconciled.archived == imported.archived
    assert reconciled.live_revision_id == imported.live_revision_id
    assert reconciled.revisions[str(pending.id)].commit_sha == committed
    assert reconciled.revisions[str(pending.id)].state == "superseded"
    assert await repository.head() == head


@pytest.mark.parametrize("lost_response", [False, True])
async def test_imported_deletion_can_be_restored_without_repository_repair(
    lost_response: bool,
) -> None:
    """Both acknowledged and pending first publications retain a usable archive parent."""
    factory, service, repository, _ = await setup()
    owner = principal()
    first = await service.write(
        principal=owner,
        slug="count-items",
        source="def main(inputs, job): return 1",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="first",
    )
    write = repository.write_widget

    async def lose_response(*args: Any, **kwargs: Any) -> str:
        await write(*args, **kwargs)
        raise WidgetRepositoryUnavailableError("Lost response")

    if lost_response:
        repository.write_widget = lose_response  # type: ignore[method-assign]
    await service.flush(owner)
    repository.write_widget = write  # type: ignore[method-assign]
    head = await repository.head()
    assert head is not None
    deleted = await repository.remove_widget("count-items", parent_sha=head, message="Stop job")
    await service.import_changes(owner)
    cast(FixedClock, service.clock).advance(timedelta(minutes=1))
    await service.flush(owner)
    async with factory() as uow:
        archived = await uow.jobs.get(owner, "count-items")
    assert archived.archived
    restored = await service.write(
        principal=owner,
        slug="count-items",
        source=first.source,
        manifest=first.manifest,
        expected_revision=archived.live_revision_id,
        request_key="restore",
    )
    assert await service.flush(owner) == 1
    async with factory() as uow:
        saved = await uow.jobs.get(owner, "count-items")
    assert not saved.archived
    assert saved.live_revision_id == restored.id
    assert saved.revisions[str(restored.id)].state == "live"
    assert archived.live_revision_id is not None
    assert archived.revisions[str(archived.live_revision_id)].commit_sha == deleted
    assert saved.revisions[str(first.id)].source == first.source


async def test_another_owners_backlog_cannot_starve_job_publication() -> None:
    """Owner filtering happens before the maintenance batch limit."""
    from uuid import uuid4

    from agent_core.domain.cockpit import CommitOutboxEntry, OutboxOperation

    factory, service, _, _ = await setup()
    owner = principal()
    earlier = service.clock.now() - timedelta(minutes=1)
    async with factory() as uow:
        for _ in range(100):
            await uow.job_outbox.enqueue(
                CommitOutboxEntry(
                    tenant_id=owner.tenant_id,
                    principal_id="another-owner",
                    revision_id=uuid4(),
                    slug="other-job",
                    operation=OutboxOperation.WRITE,
                    next_attempt_at=earlier,
                    created_at=earlier,
                )
            )
    revision = await service.write(
        principal=owner,
        slug="count-items",
        source="def main(inputs, job): return 1",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="save",
    )
    assert await service.flush(owner) == 1
    async with factory() as uow:
        saved = await uow.jobs.get(owner, "count-items")
        remaining = await uow.job_outbox.due(owner.tenant_id, now=service.clock.now(), limit=100)
    assert saved.live_revision_id == revision.id
    assert len(remaining) == 100
    assert all(item.principal_id == "another-owner" for item in remaining)


async def test_new_private_authoring_inputs_are_screened_before_first_publication() -> None:
    """New inputs are private even before a saved grant exists."""
    _, service, repository, screen = await setup()
    await service.write(
        principal=principal(),
        slug="count-items",
        source="def main(inputs, job): return 3",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="private-first",
        private_inputs={"account": "private-canary-account"},
    )
    assert "private-canary-account" in screen.seen[0].settings_values
    assert await repository.head() is None


async def test_publication_rechecks_privacy_before_outbox_delivery() -> None:
    """A value discovered after preparation must still block the public write."""
    factory, service, repository, screen = await setup()
    await service.write(
        principal=principal(),
        slug="count-items",
        source="def main(inputs, job): return 3",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="queued",
    )
    screen.reject = True
    assert await service.flush(principal()) == 0
    assert await repository.head() is None
    async with factory() as uow:
        saved = await uow.jobs.get(principal(), "count-items")
    assert saved.live_revision_id is None


async def test_privacy_rejection_is_terminal_until_owner_supplies_corrected_code() -> None:
    """Maintenance cannot later publish a rejected package merely because inputs changed."""
    factory, service, repository, screen = await setup()
    revision = await service.write(
        principal=principal(),
        slug="count-items",
        source="def main(inputs, job): return 3",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="terminal",
    )
    screen.reject = True
    assert await service.flush(principal()) == 0
    screen.reject = False
    cast(FixedClock, service.clock).advance(timedelta(minutes=1))
    assert await service.flush(principal()) == 0
    assert await repository.head() is None
    async with factory() as uow:
        saved = await uow.jobs.get(principal(), "count-items")
    assert saved.revisions[str(revision.id)].state == "rejected"


async def test_new_privacy_match_does_not_block_removal_of_public_code() -> None:
    """Archiving publishes only a fixed commit message, never the old package."""
    _, service, repository, _ = await setup()
    owner = principal()
    revision = await service.write(
        principal=owner,
        slug="count-items",
        source="def main(inputs, job): return 3",
        manifest=JobManifest(title="Count items"),
        expected_revision=None,
        request_key="publish",
    )
    assert await service.flush(owner) == 1

    class RejectOldSource(Screen):
        async def check(
            self, principal: Principal, files: Mapping[str, str], *, screen: BuildScreen
        ) -> tuple[BuildDiagnostic, ...]:
            """Reject old private source while allowing the fixed archive message."""
            return (
                (BuildDiagnostic(rule="screen.setting", message="Contains a private value."),)
                if "return 3" in files.get("main.py", "")
                else ()
            )

    service.screen = RejectOldSource()
    await service.write(
        principal=owner,
        slug="count-items",
        source=revision.source,
        manifest=revision.manifest,
        expected_revision=revision.id,
        request_key="archive",
        archive=True,
    )
    assert await service.flush(owner) == 1
    head = await repository.head()
    assert head is not None
    assert await repository.read_widget("count-items", at=head) is None
