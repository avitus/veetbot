"""Structured belief-store contract."""

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.memory.in_memory import InMemoryMemoryStore
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import (
    BeliefRejection,
    BeliefType,
    ConsolidationRun,
    MemoryEdit,
    MemoryStatus,
    RejectionKind,
    Sensitivity,
)
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.support import NOW, PRINCIPAL_ID, SESSION_ID, TENANT, principal


def _store() -> InMemoryMemoryStore:
    return InMemoryMemoryStore(FixedClock(NOW))


async def test_memory_store_enforces_scope_and_lifecycle() -> None:
    store = _store()
    value = memory()
    await store.upsert_belief(value)
    assert await store.query(recall_query()) == [value]
    assert await store.query(recall_query(tenant_id="tenant-b")) == []
    with pytest.raises(NotFoundError):
        await store.get(value.id, principal().model_copy(update={"principal_id": "other"}))


async def test_upsert_is_idempotent_but_rejects_content_drift() -> None:
    store = _store()
    value = memory()
    await store.upsert_belief(value)
    await store.upsert_belief(value)
    assert await store.query(recall_query()) == [value]
    with pytest.raises(ConflictError):
        await store.upsert_belief(value.model_copy(update={"statement": "Different content"}))


async def test_reinforce_requires_an_existing_belief() -> None:
    store = _store()
    with pytest.raises(NotFoundError):
        await store.reinforce(memory())
    await store.upsert_belief(memory())
    reinforced = memory().model_copy(update={"corroboration_count": 2, "store_position": 2})
    await store.reinforce(reinforced)
    assert (await store.get(reinforced.id, principal())).corroboration_count == 2


async def test_supersede_retires_the_current_belief_exactly_once() -> None:
    store = _store()
    value = memory()
    await store.upsert_belief(value)
    replacement = memory(belief_id=502, statement="User prefers detailed answers").model_copy(
        update={"store_position": 2}
    )
    superseded = value.model_copy(
        update={
            "status": MemoryStatus.SUPERSEDED,
            "valid_to": NOW,
            "superseded_by": replacement.id,
            "store_position": 3,
        }
    )
    await store.supersede(superseded, replacement)

    stored = await store.get(value.id, principal())
    assert stored.status is MemoryStatus.SUPERSEDED
    assert stored.superseded_by == replacement.id
    assert stored.valid_to is not None
    live = await store.query(recall_query(text="answers"))
    assert [record.id for record in live] == [replacement.id]
    with pytest.raises(ConflictError):
        await store.supersede(superseded, replacement)


async def test_related_matches_subject_casefold_and_live_records_only() -> None:
    store = _store()
    await store.upsert_belief(memory())
    retired = memory(belief_id=502, statement="Old belief about answers").model_copy(
        update={"status": MemoryStatus.RETIRED, "valid_to": NOW, "store_position": 2}
    )
    await store.upsert_belief(retired)
    related = await store.related(TENANT, PRINCIPAL_ID, "ANSWER STYLE", BeliefType.PREFERENCE)
    assert [record.id for record in related] == [memory().id]
    assert await store.related(TENANT, PRINCIPAL_ID, "answer style", BeliefType.FACT) == []


async def test_list_memories_orders_by_recency_and_honors_liveness_and_limit() -> None:
    store = _store()
    first = memory(belief_id=511).model_copy(update={"store_position": 1})
    second = memory(belief_id=512, statement="User prefers tabs").model_copy(
        update={"subject": "indentation", "store_position": 2}
    )
    retired = memory(belief_id=513, statement="Old retired belief").model_copy(
        update={
            "subject": "history",
            "status": MemoryStatus.RETIRED,
            "valid_to": NOW,
            "store_position": 3,
        }
    )
    for record in (first, second, retired):
        await store.upsert_belief(record)

    live = await store.list_memories(principal())
    assert [record.id for record in live] == [second.id, first.id]
    everything = await store.list_memories(principal(), include_inactive=True)
    assert [record.id for record in everything] == [retired.id, second.id, first.id]
    limited = await store.list_memories(principal(), include_inactive=True, limit=1)
    assert [record.id for record in limited] == [retired.id]
    other_session = second.model_copy(
        update={"id": UUID(int=514), "source_session_id": UUID(int=999), "store_position": 4}
    )
    await store.upsert_belief(other_session)
    session_only = await store.list_memories(
        principal(), include_inactive=True, session_id=SESSION_ID
    )
    assert [record.id for record in session_only] == [retired.id, second.id, first.id]


async def test_edit_and_delete_are_principal_scoped() -> None:
    store = _store()
    value = memory()
    await store.upsert_belief(value)
    foreign = principal().model_copy(update={"principal_id": "principal-b"})
    edited = value.model_copy(update={"statement": "Edited statement", "store_position": 2})
    edit = MemoryEdit(statement="Edited statement")
    with pytest.raises(NotFoundError):
        await store.edit(value.id, foreign, edit, edited)
    tombstone = BeliefRejection(
        id=UUID(int=901),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        belief_id=value.id,
        kind=RejectionKind.DELETED,
        subject=value.subject,
        statement=None,
        statement_sha256="0" * 64,
        belief_type=value.belief_type,
        scope=value.scope,
        created_at=NOW,
    )
    with pytest.raises(NotFoundError):
        await store.delete(value.id, foreign, tombstone)

    await store.edit(value.id, principal(), edit, edited)
    assert (await store.get(value.id, principal())).statement == "Edited statement"
    await store.delete(value.id, principal(), tombstone)
    with pytest.raises(NotFoundError):
        await store.get(value.id, principal())
    rejections = await store.outstanding_rejections(TENANT, PRINCIPAL_ID)
    assert [rejection.id for rejection in rejections] == [tombstone.id]
    assert await store.outstanding_rejections(TENANT, "principal-b") == []


async def test_consolidation_watermark_is_monotonic_and_principal_scoped() -> None:
    store = _store()
    assert await store.consolidation_watermark(SESSION_ID, principal()) == 0
    await store.set_consolidation_watermark(SESSION_ID, principal(), 5)
    await store.set_consolidation_watermark(SESSION_ID, principal(), 3)
    assert await store.consolidation_watermark(SESSION_ID, principal()) == 5
    foreign = principal().model_copy(update={"principal_id": "principal-b"})
    assert await store.consolidation_watermark(SESSION_ID, foreign) == 0


async def test_consolidation_runs_are_inspectable_and_principal_scoped() -> None:
    store = _store()
    older = ConsolidationRun(
        id=UUID(int=801),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
        watermark_before=0,
        watermark_after=5,
        model="deterministic-formation-v2",
        policy_version="formation@2",
        candidates_proposed=1,
        committed=1,
        reinforced=0,
        superseded=0,
        rejected=0,
        started_at=NOW - timedelta(minutes=1),
        finished_at=NOW - timedelta(minutes=1),
    )
    newer = older.model_copy(
        update={
            "id": UUID(int=802),
            "watermark_before": 5,
            "watermark_after": 9,
            "started_at": NOW,
            "finished_at": NOW,
        }
    )
    foreign = older.model_copy(
        update={"id": UUID(int=803), "principal_id": "principal-b", "started_at": NOW}
    )
    for run in (older, newer, foreign):
        await store.record_consolidation(run)

    assert await store.list_consolidations(principal(), session_id=SESSION_ID) == [newer, older]
    assert await store.list_consolidations(principal(), limit=1) == [newer]
    assert await store.list_consolidations(
        principal().model_copy(update={"principal_id": "principal-b"})
    ) == [foreign]


async def test_expire_retires_only_live_records_past_their_expiry() -> None:
    store = _store()
    past = memory(belief_id=521).model_copy(
        update={"expires_at": NOW - timedelta(seconds=1), "store_position": 1}
    )
    future = memory(belief_id=522, statement="User prefers tabs").model_copy(
        update={
            "subject": "indentation",
            "expires_at": NOW + timedelta(days=1),
            "store_position": 2,
        }
    )
    already_retired = memory(belief_id=523, statement="Old retired belief").model_copy(
        update={
            "subject": "history",
            "status": MemoryStatus.RETIRED,
            "valid_to": NOW,
            "expires_at": NOW - timedelta(days=1),
            "store_position": 3,
        }
    )
    for record in (past, future, already_retired):
        await store.upsert_belief(record)

    expired = await store.expire(principal())
    assert [record.id for record in expired] == [past.id]
    assert expired[0].status is MemoryStatus.EXPIRED
    assert expired[0].valid_to == NOW
    live = await store.query(recall_query(text=None, subjects=["answer style", "indentation"]))
    assert [record.id for record in live] == [future.id]


async def test_query_applies_ceiling_type_and_bitemporal_filters() -> None:
    store = _store()
    restricted = memory(belief_id=531).model_copy(
        update={"sensitivity": Sensitivity.RESTRICTED, "store_position": 1}
    )
    await store.upsert_belief(restricted)
    assert await store.query(recall_query(sensitivity_ceiling=Sensitivity.INTERNAL)) == []
    assert [
        record.id
        for record in await store.query(recall_query(sensitivity_ceiling=Sensitivity.RESTRICTED))
    ] == [restricted.id]
    assert await store.query(recall_query(belief_types=[BeliefType.FACT])) == []

    superseded = memory(belief_id=532, statement="User preferred terse notes").model_copy(
        update={
            "subject": "notes",
            "status": MemoryStatus.SUPERSEDED,
            "valid_from": NOW - timedelta(days=2),
            "valid_to": NOW - timedelta(days=1),
            "superseded_by": UUID(int=533),
            "store_position": 2,
        }
    )
    await store.upsert_belief(superseded)
    assert superseded.id not in {record.id for record in await store.query(recall_query())}
    historical = await store.query(recall_query(as_of=NOW - timedelta(days=1, hours=12)))
    assert [record.id for record in historical] == [superseded.id]
    # Bi-temporal validity outranks include_superseded: a belief whose validity
    # ended is not live even when the caller asks to see superseded records.
    included = await store.query(recall_query(include_superseded=True))
    assert superseded.id not in {record.id for record in included}
