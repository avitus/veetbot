"""Faithful recall-trace storage contract."""

import hashlib
from uuid import UUID

import pytest

from agent_core.adapters.memory.in_memory import InMemoryTraceStore
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import RecallMoment, Sensitivity, TracedPassage
from tests.contract.memory_fixtures import recalled, trace
from tests.contract.support import RUN_ID, principal


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
