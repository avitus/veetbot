"""Faithful recall-trace storage contract."""

from uuid import UUID

from agent_core.adapters.memory.in_memory import InMemoryTraceStore
from agent_core.domain.memory import Sensitivity, TracedPassage
from tests.contract.memory_fixtures import trace
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
