"""Job storage contract shared by memory and PostgreSQL."""

from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest

from agent_core.adapters.persistence.jobs import InMemoryJobStore
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.jobs import JobAccess, JobGrant, JobManifest, JobRecord, JobRevision
from agent_core.ports.jobs import JobStore
from tests.contract.support import NOW, principal

SCENARIOS: list[Callable[[JobStore, Principal], Awaitable[None]]] = []


def scenario(
    function: Callable[[JobStore, Principal], Awaitable[None]],
) -> Callable[[JobStore, Principal], Awaitable[None]]:
    SCENARIOS.append(function)
    return function


def record(owner: Principal) -> JobRecord:
    revision = JobRevision(
        id=UUID(int=1),
        manifest=JobManifest(title="Count items"),
        source="def main(inputs, job): return len(inputs['items'])",
        state="live",
        commit_sha="a" * 40,
        created_at=NOW,
    )
    grant = JobGrant(id=UUID(int=2), access=JobAccess({"items": [1, 2]}, {}), created_at=NOW)
    return JobRecord(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        slug="count-items",
        version=1,
        live_revision_id=revision.id,
        revisions={str(revision.id): revision},
        grants={str(grant.id): grant},
        updated_at=NOW,
    )


@scenario
async def job_and_its_private_grants_are_owner_scoped(store: JobStore, owner: Principal) -> None:
    saved = record(owner)
    await store.put(saved, expected_version=None)
    assert await store.get(owner, saved.slug) == saved
    other = owner.model_copy(update={"principal_id": "another-owner"})
    with pytest.raises(NotFoundError):
        await store.get(other, saved.slug)
    assert await store.list(other) == []
    assert await store.erase_principal(other) == 0
    assert await store.get(owner, saved.slug) == saved


@scenario
async def edits_compare_the_expected_version_and_keep_pinned_source(
    store: JobStore, owner: Principal
) -> None:
    saved = record(owner)
    await store.put(saved, expected_version=None)
    changed = saved.model_copy(update={"version": 2, "archived": True})
    with pytest.raises(ConflictError):
        await store.put(changed, expected_version=None)
    with pytest.raises(ConflictError):
        await store.put(changed, expected_version=9)
    await store.put(changed, expected_version=1)
    actual = await store.get(owner, saved.slug)
    assert actual.archived
    assert actual.revisions == saved.revisions
    assert actual.grants == saved.grants
    with pytest.raises(ConflictError):
        await store.put(saved, expected_version=1)
    assert await store.erase_principal(owner) == 1
    assert await store.list(owner) == []


@scenario
async def callers_cannot_mutate_a_stored_grant_by_changing_their_copy(
    store: JobStore, owner: Principal
) -> None:
    saved = record(owner)
    await store.put(saved, expected_version=None)
    saved.grants[str(UUID(int=2))].access.inputs["items"] = [99]
    read = await store.get(owner, saved.slug)
    assert read.grants[str(UUID(int=2))].access.inputs["items"] == [1, 2]
    read.revisions.clear()
    assert (await store.get(owner, saved.slug)).revisions


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda function: function.__name__)
async def test_memory_job_store(check: Callable[[JobStore, Principal], Awaitable[None]]) -> None:
    await check(InMemoryJobStore(), principal())
