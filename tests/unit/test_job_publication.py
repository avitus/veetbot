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
