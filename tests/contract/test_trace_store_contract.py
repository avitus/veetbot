"""Faithful recall-trace storage contract."""

import hashlib
from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.memory.in_memory import InMemoryTraceStore
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import RecallMoment, Sensitivity, TracedPassage
from tests.contract.memory_fixtures import recalled, trace
from tests.contract.support import NOW, RUN_ID, principal


async def test_trace_store_round_trips_the_same_record() -> None:
    store = InMemoryTraceStore()
    value = trace()
    await store.record(value)
    assert await store.get(value.id, principal()) == value
    assert await store.for_turn(RUN_ID) == [value]


async def test_trace_redaction_is_tenant_scoped() -> None:
    store = InMemoryTraceStore()
    document_id = UUID(int=700)
    passage = TracedPassage(
        chunk_id="kc_0000000000000700",
        document_id=document_id,
        title="Guide",
        heading_path=[],
        text="tenant-specific text",
        sensitivity=Sensitivity.INTERNAL,
    )
    local = trace().model_copy(update={"passages": [passage]})
    foreign = trace().model_copy(
        update={
            "id": UUID(int=701),
            "tenant_id": "tenant-b",
            "principal_id": "principal-b",
            "passages": [passage],
        }
    )
    await store.record(local)
    await store.record(foreign)

    await store.mark_document_deleted(local.tenant_id, document_id)

    redacted = await store.get(local.id, principal())
    visible = await store.get(
        foreign.id,
        principal().model_copy(update={"tenant_id": "tenant-b", "principal_id": "principal-b"}),
    )
    assert redacted.passages[0].deleted is True
    assert redacted.passages[0].text is None
    assert visible.passages[0].deleted is False
    assert visible.passages[0].text == passage.text


async def test_record_rejects_conflicting_content_for_the_same_id() -> None:
    store = InMemoryTraceStore()
    await store.record(trace())
    await store.record(trace())
    drifted = trace().model_copy(
        update={
            "rendered": "<memory>drift</memory>",
            "rendered_sha256": hashlib.sha256(b"<memory>drift</memory>").hexdigest(),
        }
    )
    with pytest.raises(ConflictError):
        await store.record(drifted)


async def test_user_view_applies_the_viewing_ceiling_and_reports_withholdings() -> None:
    store = InMemoryTraceStore()
    internal = recalled(belief_id=611, statement="Internal preference")
    restricted = recalled(
        belief_id=612,
        statement="Restricted preference",
        subject="secrets policy",
        sensitivity=Sensitivity.RESTRICTED,
    )
    blocked = recalled(
        belief_id=613,
        statement="[BLOCKED]",
        subject="poisoned entry",
        blocked=True,
    )
    recorded = trace().model_copy(
        update={
            "sensitivity_ceiling": Sensitivity.RESTRICTED,
            "beliefs": [internal, restricted, blocked],
            "returned": [internal.belief_id, restricted.belief_id, blocked.belief_id],
            "cited": [internal.belief_id],
            "blocked": [blocked.belief_id],
            "dropped_for_budget": [UUID(int=777)],
        }
    )
    await store.record(recorded)

    view = await store.user_view(RUN_ID, "shared", "internal")

    assert view.moments == [RecallMoment.IN_TURN]
    assert [belief.belief_id for belief in view.beliefs] == [internal.belief_id]
    assert view.beliefs[0].used is True
    assert view.considered_not_shown == 1
    assert view.withheld_by_safety == 1


async def test_user_view_is_bounded_by_the_recall_time_ceiling_too() -> None:
    store = InMemoryTraceStore()
    internal = recalled(belief_id=621, statement="Internal preference")
    restricted = recalled(
        belief_id=622,
        statement="Restricted preference",
        subject="secrets policy",
        sensitivity=Sensitivity.RESTRICTED,
    )
    recorded = trace().model_copy(
        update={
            "sensitivity_ceiling": Sensitivity.INTERNAL,
            "beliefs": [internal, restricted],
            "returned": [internal.belief_id, restricted.belief_id],
        }
    )
    await store.record(recorded)

    view = await store.user_view(RUN_ID, "private", "restricted")

    assert [belief.belief_id for belief in view.beliefs] == [internal.belief_id]


async def test_expire_operator_fields_nulls_only_expired_operator_tier() -> None:
    """The operator tier is swept on its own clock; the user tier survives it.

    Two traces are past their operator expiry and one is not, so the sweep is
    observed to be bounded by its limit, ordered oldest-expiry-first, blind to
    the unexpired trace, and idempotent once every expired trace is clean.
    """

    store = InMemoryTraceStore()
    kept = recalled(belief_id=641, statement="Kept preference")
    operator_tier = {
        "arm_latencies_ms": {"structured": 3, "lexical": 5},
        "candidates": 7,
        "beliefs": [kept],
        "returned": [kept.belief_id],
        "cited": [kept.belief_id],
        "dropped_for_budget": [UUID(int=642)],
    }
    oldest = trace().model_copy(
        update={**operator_tier, "operator_fields_expire_at": NOW - timedelta(days=2)}
    )
    older = trace().model_copy(
        update={
            **operator_tier,
            "id": UUID(int=643),
            "operator_fields_expire_at": NOW - timedelta(days=1),
        }
    )
    unexpired = trace().model_copy(
        update={
            **operator_tier,
            "id": UUID(int=644),
            "operator_fields_expire_at": NOW + timedelta(days=1),
        }
    )
    for value in (older, unexpired, oldest):
        await store.record(value)

    assert await store.expire_operator_fields(NOW, 1) == 1
    assert (await store.get(older.id, principal())).candidates == 7
    swept = await store.get(oldest.id, principal())
    assert swept.arm_latencies_ms == {}
    assert swept.candidates == 0
    assert swept.dropped_for_budget == []
    assert swept.dropped_for_budget_count == 1
    assert swept.beliefs == [kept]
    assert swept.returned == [kept.belief_id]
    assert swept.cited == [kept.belief_id]
    assert swept.rendered == oldest.rendered
    assert swept.rendered_sha256 == oldest.rendered_sha256

    assert await store.expire_operator_fields(NOW, 10) == 1
    assert (await store.get(older.id, principal())).arm_latencies_ms == {}
    assert await store.get(unexpired.id, principal()) == unexpired
    assert await store.expire_operator_fields(NOW, 10) == 0

    # The user-safe projection still reports how many beliefs were considered
    # and not shown: from the surviving count once the identifiers are gone,
    # and from the identifiers while the trace still carries them.
    view = await store.user_view(RUN_ID, "private", "restricted")
    assert view.considered_not_shown == 3


async def test_mark_cited_marks_used_and_is_principal_scoped_and_idempotent() -> None:
    """Citation unions into the trace, is scoped to its owner, and repeats cleanly.

    A trace returns two beliefs and the answer cites one, so the mark is
    observed changing the user view of exactly that belief; repeating the same
    mark changes nothing, a later mark widens the set rather than replacing it,
    and neither a foreign principal nor an unknown identifier can reach it.
    """

    store = InMemoryTraceStore()
    used = recalled(belief_id=651, statement="Used preference")
    unused = recalled(belief_id=652, statement="Unused preference", subject="theme")
    value = trace().model_copy(
        update={"beliefs": [used, unused], "returned": [used.belief_id, unused.belief_id]}
    )
    await store.record(value)

    marked = await store.mark_cited(value.id, principal(), [used.belief_id])
    assert marked.cited == [used.belief_id]
    assert (await store.get(value.id, principal())).cited == [used.belief_id]
    assert await store.mark_cited(value.id, principal(), [used.belief_id]) == marked

    view = await store.user_view(RUN_ID, "private", "restricted")
    assert {belief.belief_id: belief.used for belief in view.beliefs} == {
        used.belief_id: True,
        unused.belief_id: False,
    }

    foreign = principal().model_copy(update={"principal_id": "principal-b"})
    with pytest.raises(NotFoundError):
        await store.mark_cited(value.id, foreign, [used.belief_id])
    with pytest.raises(NotFoundError):
        await store.mark_cited(UUID(int=659), principal(), [used.belief_id])

    widened = await store.mark_cited(value.id, principal(), [unused.belief_id])
    assert widened.cited == [used.belief_id, unused.belief_id]
