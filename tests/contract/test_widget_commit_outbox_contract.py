"""Widget commit outbox contract, shared by the in-memory and PostgreSQL adapters."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.persistence.cockpit import InMemoryWidgetCommitOutbox
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    BuildDiagnostic,
    CommitOutboxEntry,
    ImportFailure,
    OutboxOperation,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.cockpit import WidgetCommitOutbox
from tests.contract.cockpit_support import COMMIT_A, COMMIT_B, Scenario, foreign, scenarios
from tests.contract.support import NOW, principal

SCENARIOS, scenario = scenarios()


def entry(
    owner: Principal,
    *,
    revision_id: UUID | None = None,
    due_in_minutes: int = 0,
    created_minutes: int = 0,
) -> CommitOutboxEntry:
    return CommitOutboxEntry(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        revision_id=revision_id or uuid4(),
        slug="weather",
        operation=OutboxOperation.WRITE,
        next_attempt_at=NOW + timedelta(minutes=due_in_minutes),
        expected_parent_sha=COMMIT_A,
        created_at=NOW + timedelta(minutes=created_minutes),
    )


def failure(owner: Principal, *, minutes: int = 0, commit: str = COMMIT_A) -> ImportFailure:
    return ImportFailure(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        commit_sha=commit,
        slug="weather",
        diagnostics=(BuildDiagnostic(rule="lint.banned", file="widget.tsx", message="x"),),
        created_at=NOW + timedelta(minutes=minutes),
    )


async def _due(outbox: WidgetCommitOutbox, owner: Principal, minutes: int) -> list[UUID]:
    return [
        item.revision_id
        for item in await outbox.due(
            owner.tenant_id, now=NOW + timedelta(minutes=minutes), limit=10
        )
        if item.principal_id == owner.principal_id
    ]


@scenario
async def due_entries_come_oldest_first(outbox: WidgetCommitOutbox, owner: Principal) -> None:
    later = await outbox.enqueue(entry(owner, due_in_minutes=5, created_minutes=1))
    first = await outbox.enqueue(entry(owner, created_minutes=2))
    second = await outbox.enqueue(entry(owner, created_minutes=3))
    assert await _due(outbox, owner, 0) == [first.revision_id, second.revision_id]
    assert await _due(outbox, owner, 5) == [
        first.revision_id,
        second.revision_id,
        later.revision_id,
    ]
    with pytest.raises(ConflictError) as error:
        await outbox.enqueue(first)
    assert error.value.reason == "commit_queued"


@scenario
async def a_retry_is_rescheduled_and_completion_removes_it(
    outbox: WidgetCommitOutbox, owner: Principal
) -> None:
    queued = await outbox.enqueue(entry(owner))
    retried = await outbox.reschedule(
        owner,
        queued.revision_id,
        attempts=1,
        next_attempt_at=NOW + timedelta(minutes=2),
        last_error="github unavailable",
    )
    assert (retried.attempts, retried.last_error) == (1, "github unavailable")
    assert await outbox.first_for_widget(owner, "weather") == retried
    assert await outbox.first_for_widget(foreign(owner), "weather") is None
    assert await outbox.first_for_widget(owner, "markets") is None
    assert await _due(outbox, owner, 1) == []
    assert await _due(outbox, owner, 2) == [queued.revision_id]
    with pytest.raises(NotFoundError):
        await outbox.reschedule(
            foreign(owner),
            queued.revision_id,
            attempts=2,
            next_attempt_at=NOW,
            last_error=None,
        )
    await outbox.complete(owner, queued.revision_id)
    await outbox.complete(owner, queued.revision_id)
    assert await _due(outbox, owner, 10) == []


@scenario
async def import_failures_are_listed_newest_first_until_seen(
    outbox: WidgetCommitOutbox, owner: Principal
) -> None:
    older = await outbox.record_import_failure(failure(owner, minutes=1))
    newer = await outbox.record_import_failure(failure(owner, minutes=2, commit=COMMIT_B))
    assert [item.id for item in await outbox.import_failures(owner)] == [newer.id, older.id]
    assert await outbox.mark_seen(owner, [older.id]) == 1
    assert await outbox.mark_seen(foreign(owner), [newer.id]) == 0
    assert [item.id for item in await outbox.import_failures(owner, unseen_only=True)] == [newer.id]
    assert await outbox.import_failures(foreign(owner)) == []


@scenario
async def erasure_removes_only_the_owners_outbox(
    outbox: WidgetCommitOutbox, owner: Principal
) -> None:
    await outbox.enqueue(entry(owner))
    await outbox.record_import_failure(failure(owner))
    kept = await outbox.enqueue(entry(foreign(owner)))
    assert await outbox.erase_principal(owner) == 2
    assert await _due(outbox, owner, 10) == []
    assert await outbox.import_failures(owner) == []
    assert await _due(outbox, foreign(owner), 10) == [kept.revision_id]


@scenario
async def principal_filter_precedes_limit_without_changing_tenant_wide_reads(
    outbox: WidgetCommitOutbox, owner: Principal
) -> None:
    """A different owner's older entry cannot consume a principal-scoped page."""
    other = owner.model_copy(update={"principal_id": f"other-{owner.principal_id}"})
    earlier = await outbox.enqueue(entry(other, due_in_minutes=-60, created_minutes=-60))
    mine = await outbox.enqueue(entry(owner))
    assert await outbox.due(owner.tenant_id, principal_id=owner.principal_id, now=NOW, limit=1) == [
        mine
    ]
    assert await outbox.due(owner.tenant_id, now=NOW, limit=1) == [earlier]
    await outbox.complete(other, earlier.revision_id)
    await outbox.complete(owner, mine.revision_id)


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda check: check.__name__)
async def test_in_memory_widget_commit_outbox(check: Scenario) -> None:
    await check(InMemoryWidgetCommitOutbox(), principal())


async def test_due_entries_are_scoped_to_one_tenant() -> None:
    outbox = InMemoryWidgetCommitOutbox()
    owner = principal()
    other = owner.model_copy(update={"tenant_id": "tenant-b"})
    await outbox.enqueue(entry(other))
    assert await outbox.due(owner.tenant_id, now=NOW, limit=10) == []
