"""Summary recall preserves atoms, attribution and generated-copy erasure."""

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    MemoryRecord,
    RecallProfile,
    RecallQuery,
    Sensitivity,
    UsageFeedback,
)
from agent_core.domain.reconsolidation_operations import StoredSummary
from agent_core.domain.reconsolidation_summary import SummaryClause
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.support import NOW, SESSION_ID, principal


async def summary_recall_inputs(
    factory: UnitOfWorkFactory,
    *,
    omit_last: bool = False,
    duplicate_first: bool = False,
    expires_at: datetime | None = None,
    people: tuple[int, int] | None = None,
) -> tuple[StoredSummary, list[MemoryRecord]]:
    async with factory() as uow:
        statements = tuple(
            f"User prefers option {0 if duplicate_first and n == 3 else n}" for n in range(5)
        )
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={"content": ". ".join(statements)},
            )
        )
        originals = []
        for i, statement in enumerate(statements):
            original = memory(belief_id=501 + i).model_copy(
                update={
                    "statement": statement,
                    "utility": 1.0 if i < 3 else -1.0,
                    "expires_at": expires_at if i == 4 else None,
                    "store_position": await uow.memories.next_position(),
                }
            )
            originals.append(original)
            await uow.memories.upsert_belief(original)
        if people is not None:
            from agent_core.domain.people_sources import source_id
            from tests.contract.reconsolidation_attribution_cases import link, person, source

            leaf = source().model_copy(update={"id": source_id(principal(), SESSION_ID, 1)})
            async with uow.people.lock(principal()):
                await uow.people.put(leaf, expected_revision=0)
                for key in sorted(set(people)):
                    await uow.people.put(person(key), expected_revision=0)
                for index, key in enumerate(people):
                    await uow.people.put(
                        link(504 + index).model_copy(
                            update={
                                "id": UUID(int=811 + index),
                                "person_id": person(key).id,
                                "support_ids": [leaf.id],
                            }
                        ),
                        expected_revision=0,
                    )
        job = await uow.reconsolidation.claim_due(principal(), NOW, "summary-recall")
        assert job is not None
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        await uow.reconsolidation.checkpoint(
            principal(), job.lease_token, page, (page.sources[3:],), NOW
        )
        group = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
        assert group is not None
        clauses = tuple(
            SummaryClause(text=r.statement, source_ids=(r.id,))
            for r in originals[3 : 4 if omit_last else 5]
        )
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
        operation = await uow.reconsolidation.commit_summary(
            principal(), job.lease_token, group.id, prepared, NOW
        )
    return operation, originals


async def summaries_fill_remaining_slots(factory: UnitOfWorkFactory) -> None:
    operation, originals = await summary_recall_inputs(factory)
    retriever = HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    query = recall_query().model_copy(
        update={"text": None, "profile": RecallProfile.CORE, "max_items": 4, "budget_tokens": 2000}
    )
    result = await retriever.recall(query, session_id=SESSION_ID, moment="snapshot")
    assert [item.belief_id for item in result.items[:3]] == [item.id for item in originals[:3]]
    assert operation.id in {item.belief_id for item in result.items}, (
        "a supported summary should fill a remaining slot after atomic recall"
    )
    assert result.items[-1].model_dump().get("record_kind") == "summary"
    assert f"[m:{str(operation.id)[:8]}]" in result.rendered
    from agent_core.evals.memory_benchmark import LabeledBelief, _noise_count, _trace_matches

    derived = result.items[-1]
    label = LabeledBelief(
        label="original",
        session="source",
        belief_type="preference",
        subjects=[derived.subject],
        statements=[derived.statement],
    )
    async with factory() as uow:
        trace = (await uow.traces.get(result.trace_id, principal())).model_copy(
            update={"beliefs": [derived], "returned": [derived.belief_id]}
        )
    assert not _trace_matches([trace], label)
    assert _noise_count([trace], [label]) == 1


def summary_retriever(factory: UnitOfWorkFactory, *, enabled: bool = True) -> HybridMemoryRetriever:
    return HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=enabled
    )


def summary_query() -> RecallQuery:
    return recall_query().model_copy(
        update={"text": None, "profile": RecallProfile.CORE, "max_items": 4, "budget_tokens": 2000}
    )


async def summary_budget_preserves_originals(factory: UnitOfWorkFactory) -> None:
    operation, _ = await summary_recall_inputs(factory)
    enabled, disabled = summary_retriever(factory), summary_retriever(factory, enabled=False)
    baseline = await disabled.recall(summary_query(), session_id=SESSION_ID)
    for query in (
        summary_query().model_copy(update={"max_items": 3}),
        summary_query().model_copy(update={"budget_tokens": baseline.tokens + 1}),
    ):
        original = await disabled.recall(query, session_id=SESSION_ID)
        result = await enabled.recall(query, session_id=SESSION_ID)
        assert [item for item in result.items if item.record_kind == "belief"] == original.items
        assert result.tokens <= query.budget_tokens
        assert operation.id not in {item.belief_id for item in result.items}


async def summary_omits_duplicate_text(factory: UnitOfWorkFactory) -> None:
    operation, _ = await summary_recall_inputs(factory, duplicate_first=True)
    result = await summary_retriever(factory).recall(summary_query(), session_id=SESSION_ID)
    assert operation.id not in {item.belief_id for item in result.items}


async def summary_delta_and_kill_switch(factory: UnitOfWorkFactory) -> None:
    operation, originals = await summary_recall_inputs(factory)
    query = summary_query().model_copy(
        update={"min_store_position": max(r.store_position for r in originals)}
    )
    result = await summary_retriever(factory).recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [operation.id]
    disabled = summary_retriever(factory, enabled=False)
    assert not (await disabled.recall(query, session_id=SESSION_ID)).items
    corrections = await disabled.corrections(
        snapshot_id=result.trace_id, watermark=result.watermark
    )
    assert [item.kind for item in corrections] == ["summary_invalidated"]
    assert (
        await disabled.recall(summary_query(), session_id=SESSION_ID)
    ).validate_snapshot_dependencies


async def summary_correction_and_trace_visibility(factory: UnitOfWorkFactory) -> None:
    operation, originals = await summary_recall_inputs(factory, omit_last=True)
    turn_id = UUID(int=991)
    retriever = summary_retriever(factory)
    result = await retriever.recall(summary_query(), session_id=SESSION_ID, turn_id=turn_id)
    service = GovernedMemoryService(factory, FixedClock(NOW), RandomIdFactory(), principal())
    assert (await service.get_recall_trace(result.trace_id)).rendered == result.rendered
    async with factory() as uow:
        view = await uow.traces.user_view(turn_id, "private", "restricted")
        derived = next(item for item in view.beliefs if item.belief_id == operation.id)
        assert derived.record_kind == "summary" and derived.operation_id == operation.id
        assert derived.source_event_id is None
        assert set(derived.support_ids) == {r.id for r in originals[3:]}
        await uow.memories.reinforce(
            originals[-1].model_copy(
                update={
                    "statement": "User no longer prefers that option",
                    "updated_at": NOW,
                    "store_position": await uow.memories.next_position(),
                }
            )
        )
    corrections = await retriever.corrections(
        snapshot_id=result.trace_id, watermark=result.watermark
    )
    assert [(item.belief_id, item.kind) for item in corrections] == [
        (operation.id, "summary_invalidated")
    ]
    async with factory() as uow:
        view = await uow.traces.user_view(turn_id, "private", "restricted")
        assert operation.id not in {item.belief_id for item in view.beliefs}
    inspected = await service.get_recall_trace(result.trace_id)
    assert operation.id not in inspected.returned
    assert operation.id not in {item.belief_id for item in inspected.beliefs}
    assert f"[m:{str(operation.id)[:8]}]" not in inspected.rendered
    async with factory() as uow:
        assert (await uow.traces.get(result.trace_id, principal())).rendered == result.rendered


async def summary_trace_erasure_follows_omitted_support(factory: UnitOfWorkFactory) -> None:
    _, originals = await summary_recall_inputs(factory, omit_last=True)
    result = await summary_retriever(factory).recall(summary_query(), session_id=SESSION_ID)
    async with factory() as uow:
        assert await uow.traces.erase_people(principal(), [], [originals[-1].id]) == 1
        with pytest.raises(NotFoundError):
            await uow.traces.get(result.trace_id, principal())


async def summary_citation_credits_only_derived_usage(factory: UnitOfWorkFactory) -> None:
    operation, originals = await summary_recall_inputs(factory)
    turn_id = UUID(int=991)
    await summary_retriever(factory).recall(summary_query(), session_id=SESSION_ID, turn_id=turn_id)
    service = GovernedMemoryService(factory, FixedClock(NOW), RandomIdFactory(), principal())
    feedback = await service.record_usage(
        session_id=SESSION_ID,
        run_id=turn_id,
        final_text=f"[m:{str(operation.id)[:8]}]",
        now=NOW + timedelta(seconds=1),
    )
    assert feedback.cited == 1
    async with factory() as uow:
        summary = await uow.reconsolidation.get_summary(
            principal(),
            operation.id,
            NOW + timedelta(seconds=1),
            ceiling=summary_query().sensitivity_ceiling,
            current_scope=summary_query().current_scope,
        )
        assert summary is not None and summary.model_dump().get("utility", 0) > 0
        assert summary.model_dump().get("last_used_at") == NOW + timedelta(seconds=1)
        assert await uow.reconsolidation.summary_operation(principal(), operation.id) == operation
        for original in originals[3:]:
            assert await uow.memories.get(original.id, principal()) == original
    duplicate = await service.record_usage(
        session_id=SESSION_ID, run_id=turn_id, final_text=f"[m:{str(operation.id)[:8]}]"
    )
    assert duplicate.cited == duplicate.uncited == 0


async def summary_expiry_corrects_frozen_trace_without_source_write(
    factory: UnitOfWorkFactory,
) -> None:
    expiry = NOW + timedelta(minutes=1)
    operation, _ = await summary_recall_inputs(factory, omit_last=True, expires_at=expiry)
    result = await summary_retriever(factory).recall(summary_query(), session_id=SESSION_ID)
    assert operation.id in {item.belief_id for item in result.items}
    expired = HybridMemoryRetriever(
        factory, FixedClock(expiry), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    async with factory() as uow:
        assert await uow.memories.head_position(principal()) == result.watermark
    corrections = await expired.corrections(snapshot_id=result.trace_id, watermark=result.watermark)
    assert [(item.belief_id, item.kind) for item in corrections] == [
        (operation.id, "summary_invalidated")
    ]
    assert corrections[0].ended_at == expiry
    result = await expired.recall(summary_query(), session_id=SESSION_ID)
    assert operation.id not in {item.belief_id for item in result.items}


async def summary_read_filters_fail_closed(factory: UnitOfWorkFactory) -> None:
    operation, originals = await summary_recall_inputs(factory)
    retriever = summary_retriever(factory)
    for changes in (
        {"sensitivity_ceiling": Sensitivity.PUBLIC},
        {"as_of": NOW - timedelta(seconds=1)},
        {"known_at": NOW - timedelta(seconds=1)},
        {"exclude_ids": (originals[-1].id,)},
        {"exclude_ids": (operation.id,)},
        {"min_score": 1.0},
        {"principal_id": "someone-else"},
    ):
        result = await retriever.recall(
            summary_query().model_copy(update=changes), session_id=SESSION_ID
        )
        assert operation.id not in {item.belief_id for item in result.items}
    result = await retriever.recall(summary_query(), session_id=SESSION_ID, turn_id=UUID(int=991))
    async with factory() as uow:
        assert not (await uow.traces.user_view(UUID(int=991), "public", "public")).beliefs
    result = await retriever.recall(
        summary_query().model_copy(update={"include_ids": tuple(r.id for r in originals[3:])}),
        session_id=SESSION_ID,
    )
    assert {item.belief_id for item in result.items} == {r.id for r in originals[3:]}


async def ambiguous_summary_citations_never_credit_support(factory: UnitOfWorkFactory) -> None:
    operation, originals = await summary_recall_inputs(factory)
    query = summary_query().model_copy(
        update={"min_store_position": max(r.store_position for r in originals)}
    )
    result = await summary_retriever(factory).recall(query, session_id=SESSION_ID)
    turn_id = UUID(int=991)
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
        # Two distinct returned UUIDs share the renderer's eight-hex citation.
        collision = UUID(int=operation.id.int ^ 1)
        await uow.traces.record(
            trace.model_copy(
                update={
                    "id": UUID(int=992),
                    "turn_id": turn_id,
                    "returned": [operation.id, collision],
                }
            )
        )
    service = GovernedMemoryService(factory, FixedClock(NOW), RandomIdFactory(), principal())
    feedback = await service.record_usage(
        session_id=SESSION_ID, run_id=turn_id, final_text=f"[m:{str(operation.id)[:8]}]"
    )
    assert feedback.ambiguous == 1 and feedback.cited == feedback.uncited == 0
    async with factory() as uow:
        summary = await uow.reconsolidation.get_summary(
            principal(),
            operation.id,
            NOW,
            ceiling=query.sensitivity_ceiling,
            current_scope=query.current_scope,
        )
        assert summary is not None and summary.utility == 0 and summary.last_used_at is None
        for original in originals:
            assert await uow.memories.get(original.id, principal()) == original


async def summary_usage_serializes_and_rolls_back(factory: UnitOfWorkFactory) -> None:
    operation, originals = await summary_recall_inputs(factory)
    query = summary_query().model_copy(
        update={"min_store_position": max(r.store_position for r in originals)}
    )
    await summary_retriever(factory).recall(query, session_id=SESSION_ID, turn_id=UUID(int=991))
    service = GovernedMemoryService(factory, FixedClock(NOW), RandomIdFactory(), principal())

    async def cite() -> UsageFeedback:
        return await service.record_usage(
            session_id=SESSION_ID, run_id=UUID(int=991), final_text=f"[m:{str(operation.id)[:8]}]"
        )

    feedback = await asyncio.gather(cite(), cite())
    assert sum(item.cited for item in feedback) == 1
    async with factory() as uow:
        before = await uow.reconsolidation.get_summary(
            principal(),
            operation.id,
            NOW,
            ceiling=query.sensitivity_ceiling,
            current_scope=query.current_scope,
        )
        head = await uow.memories.head_position(principal())
    with pytest.raises(RuntimeError, match="rollback usage"):
        async with factory() as uow:
            assert await uow.reconsolidation.update_summary_usage(
                principal(), operation.id, 0.2, NOW, cited=True
            )
            raise RuntimeError("rollback usage")
    async with factory() as uow:
        assert (
            await uow.reconsolidation.get_summary(
                principal(),
                operation.id,
                NOW,
                ceiling=query.sensitivity_ceiling,
                current_scope=query.current_scope,
            )
            == before
        )
        assert await uow.memories.head_position(principal()) == head
        await uow.memories.reinforce(
            originals[-1].model_copy(
                update={
                    "statement": "Corrected preference",
                    "store_position": await uow.memories.next_position(),
                }
            )
        )
        assert not await uow.reconsolidation.update_summary_usage(
            principal(), operation.id, 0.2, NOW, cited=True
        )


SUMMARY_RECALL_SCENARIOS = [
    summaries_fill_remaining_slots,
    summary_budget_preserves_originals,
    summary_omits_duplicate_text,
    summary_delta_and_kill_switch,
    summary_correction_and_trace_visibility,
    summary_trace_erasure_follows_omitted_support,
    summary_citation_credits_only_derived_usage,
    summary_expiry_corrects_frozen_trace_without_source_write,
    summary_read_filters_fail_closed,
    ambiguous_summary_citations_never_credit_support,
    summary_usage_serializes_and_rolls_back,
]
