from collections.abc import AsyncIterator
from unittest.mock import Mock
from uuid import UUID

import pytest

from agent_core.domain.memory import (
    MemoryRecord,
    RecalledBelief,
    RecallProfile,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.memory_sources import recovery_source_id
from agent_core.memory.source_recovery import BoundedSourceRecovery
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from tests.contract.memory_fixtures import formation_stack, memory, recall_query, user_event
from tests.contract.support import NOW, SESSION_ID, principal


async def test_source_recovery_keeps_exact_provenance_and_copy_lineage() -> None:
    _, factory, _, _ = await formation_stack()
    text = "The workshop is on Thursday. Enter through Bay 6 after 09:00."
    sequence = await user_event(factory, text)
    original = memory().model_copy(update={"source_event_ids": [sequence], "scope": "general"})
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        query = recall_query().model_copy(update={"current_scope": "general"})
        recovered = await BoundedSourceRecovery(principal(), admitted=lambda: True).recover(
            uow, query, [original], surface_id="private"
        )
    assert original.id in recovered, "Permitted source detail must survive compression"
    source = recovered[original.id][0]
    assert source.text == text and source.event_sequence == sequence
    assert source.session_id == SESSION_ID and source.created_at == NOW
    assert source.copy_id == recovery_source_id(principal(), SESSION_ID, sequence)
    assert source.copy_id != source.people_source_id
    assert source.sensitivity == "restricted"


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "foreign"},
        {"principal_id": "foreign"},
        {"current_scope": "other-project"},
        {"as_of": NOW},
        {"known_at": NOW},
        {"profile": RecallProfile.CORE},
        {"people_scope": (UUID(int=900),)},
        {"sensitivity_ceiling": Sensitivity.INTERNAL},
    ],
)
async def test_source_expansion_cannot_inherit_broader_authority_from_a_portable_fact(
    change: dict[str, object],
) -> None:
    _, factory, _, _ = await formation_stack()
    sequence = await user_event(factory, "The workshop is on Thursday.")
    original = memory().model_copy(update={"source_event_ids": [sequence]})
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        query = recall_query().model_copy(update={"current_scope": "general", **change})
        assert (
            await BoundedSourceRecovery(principal(), admitted=lambda: True).recover(
                uow, query, [original], surface_id="private"
            )
            == {}
        )


async def test_recovery_is_bounded_across_records_and_deduplicates_shared_sources() -> None:
    _, factory, _, _ = await formation_stack()
    records = []
    for index in range(10):
        sequence = await user_event(factory, f"Workshop detail {index}: " + "x" * 3900)
        records.append(
            memory(belief_id=501 + index).model_copy(
                update={
                    "source_event_ids": [sequence],
                    "scope": "general",
                    "store_position": index + 1,
                }
            )
        )
    async with factory() as uow:
        for record in records:
            await uow.memories.upsert_belief(record)
        recovered = await BoundedSourceRecovery(principal(), admitted=lambda: True).recover(
            uow,
            recall_query().model_copy(update={"current_scope": "general"}),
            [*records, records[0]],
            surface_id="private",
        )
    sources = [source for values in recovered.values() for source in values]
    assert sources, "Bounded recovery must perform admitted reads"
    assert len(sources) <= 3 and len({source.copy_id for source in sources}) == len(sources)
    assert sum(len(source.text.encode()) for source in sources) <= 8192


@pytest.mark.parametrize("budget", [100, 2000])
async def test_recovered_detail_shares_the_original_context_budget_and_citation(
    budget: int,
) -> None:
    from agent_core.adapters.determinism import SequenceIdFactory
    from agent_core.memory.retrieval import HybridMemoryRetriever

    clock, factory, _, _ = await formation_stack()
    detail = "Enter through Bay 6 after 09:00; the door is marked <workshop>."
    sequence = await user_event(factory, "The workshop is on Thursday. " + detail)
    original = memory(statement="The workshop is on Thursday.").model_copy(
        update={"subject": "workshop", "scope": "general", "source_event_ids": [sequence]}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8000, 8010)),
        principal(),
        source_recovery=BoundedSourceRecovery(principal(), admitted=lambda: True),
    )
    query = recall_query(text="workshop", budget_tokens=budget).model_copy(
        update={"current_scope": "general"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert original.statement in result.rendered
    assert result.tokens <= budget
    assert isinstance(result.items[0], RecalledBelief)
    assert result.stages is not None
    admitted = result.stages.source_candidates
    assert len(admitted) == 1
    assert admitted[0].belief_id == original.id
    assert admitted[0].session_id == SESSION_ID
    assert admitted[0].event_sequence == sequence
    assert "Enter through Bay 6" not in admitted[0].model_dump_json()
    assert "source_candidates" not in result.model_dump_json()
    if budget == 2000:
        assert "Enter through Bay 6" in result.rendered, (
            "Selected fact must carry governed supporting detail"
        )
        assert "&lt;workshop&gt;" in result.rendered
        assert result.items[0].sources
        async with factory() as uow:
            stored = await uow.traces.get(result.trace_id, principal())
        assert isinstance(stored.beliefs[0], RecalledBelief)
        assert stored.beliefs[0].sources == result.items[0].sources
    else:
        assert not result.items[0].sources, (
            "An excerpt is withheld whole when its budget is unavailable"
        )


async def test_recovered_detail_keeps_a_conflict_marker_only_beside_its_partner() -> None:
    """ADR-0248 rendering survives source recovery: re-rendering a selected
    belief with its excerpt must not restore a `conflicts=[...]` marker naming
    a partner the model was never shown."""

    from agent_core.adapters.determinism import SequenceIdFactory
    from agent_core.memory.retrieval import HybridMemoryRetriever

    clock, factory, _, _ = await formation_stack()
    sequence = await user_event(factory, "The workshop is on Thursday. Enter through Bay 6.")
    unseen = UUID("abcdef12-0000-0000-0000-000000000990")
    original = memory(statement="The workshop is on Thursday.").model_copy(
        update={
            "subject": "workshop",
            "scope": "general",
            "source_event_ids": [sequence],
            "conflicts_with": [unseen],
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8100, 8110)),
        principal(),
        source_recovery=BoundedSourceRecovery(principal(), admitted=lambda: True),
    )
    result = await retriever.recall(
        recall_query(text="workshop", budget_tokens=2000).model_copy(
            update={"current_scope": "general"}
        ),
        session_id=SESSION_ID,
    )
    assert "Enter through Bay 6" in result.rendered, "the recovery render must have run"
    assert "m:abcdef12" not in result.rendered
    # The link itself is kept for the trace and the browser.
    assert isinstance(result.items[0], RecalledBelief)
    assert result.items[0].conflict_with == [unseen]


async def test_expansion_preserves_explicit_original_selection() -> None:
    _, factory, _, _ = await formation_stack()
    sequence = await user_event(factory, "The workshop is on Thursday. Enter through Bay 6.")
    original = memory().model_copy(update={"source_event_ids": [sequence], "scope": "general"})
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        query = recall_query().model_copy(
            update={"current_scope": "general", "include_ids": (UUID(int=999),)}
        )
        assert (
            await BoundedSourceRecovery(principal(), admitted=lambda: True).recover(
                uow, query, [original], surface_id="private"
            )
            == {}
        ), "Source expansion must not widen an explicit selection"


@pytest.mark.parametrize("failure", ["error", "timeout", "cancelled"])
async def test_optional_source_failure_rolls_back_before_fact_recall(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from contextlib import asynccontextmanager

    from agent_core.adapters.determinism import SequenceIdFactory
    from agent_core.memory.retrieval import HybridMemoryRetriever

    clock, factory, _, _ = await formation_stack()
    sequence = await user_event(factory, "The workshop is on Thursday. Enter through Bay 6.")
    original = memory(statement="The workshop is on Thursday.").model_copy(
        update={"scope": "general", "source_event_ids": [sequence]}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
    closed = []
    calls = 0

    @asynccontextmanager
    async def tracked_factory() -> AsyncIterator[RepositoryUnitOfWork]:
        try:
            async with factory() as uow:
                yield uow
        except BaseException:
            closed.append("rollback")
            raise
        else:
            closed.append("commit")

    recovery = BoundedSourceRecovery(principal(), admitted=lambda: True)

    async def unavailable(
        uow: RepositoryUnitOfWork,
        query: RecallQuery,
        records: list[MemoryRecord],
        *,
        surface_id: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if failure == "cancelled":
            raise asyncio.CancelledError
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("Source storage unavailable")

    monkeypatch.setattr(recovery, "_recover", unavailable)
    retriever = HybridMemoryRetriever(
        Mock(spec=UnitOfWorkFactory, side_effect=tracked_factory),
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(9000, 9010)),
        principal(),
        source_recovery=recovery,
    )
    query = recall_query(text="workshop").model_copy(update={"current_scope": "general"})
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await retriever.recall(query, session_id=SESSION_ID)
        assert closed == ["rollback"]
    else:
        result = await retriever.recall(query, session_id=SESSION_ID)
        assert original.statement in result.rendered and "Bay 6" not in result.rendered
        assert result.arms_degraded == ["source_recovery"]
        assert closed == ["rollback", "commit"]
    assert calls == 1


@pytest.mark.parametrize("moment", ["snapshot", "child_run"])
async def test_source_quotes_never_enter_frozen_or_delegated_context(moment: str) -> None:
    from agent_core.adapters.determinism import SequenceIdFactory
    from agent_core.memory.retrieval import HybridMemoryRetriever

    clock, factory, _, _ = await formation_stack()
    sequence = await user_event(factory, "The workshop is on Thursday. Enter through Bay 6.")
    original = memory(statement="The workshop is on Thursday.").model_copy(
        update={"scope": "general", "source_event_ids": [sequence]}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(9900, 9910)),
        principal(),
        source_recovery=BoundedSourceRecovery(principal(), admitted=lambda: True),
    )
    result = await retriever.recall(
        recall_query(text="workshop").model_copy(update={"current_scope": "general"}),
        session_id=SESSION_ID,
        moment=moment,
    )
    assert original.statement in result.rendered and "Bay 6" not in result.rendered


async def test_admission_withdrawn_during_source_reads_discards_quotes() -> None:
    _, factory, _, _ = await formation_stack()
    sequence = await user_event(factory, "The workshop is on Thursday. Enter through Bay 6.")
    original = memory().model_copy(update={"scope": "general", "source_event_ids": [sequence]})
    decisions = iter([True, False])
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        recovered = await BoundedSourceRecovery(
            principal(), admitted=lambda: next(decisions)
        ).recover(
            uow,
            recall_query().model_copy(update={"current_scope": "general"}),
            [original],
            surface_id="private",
        )
    assert recovered == {}, "An in-flight read cannot outlive release admission"


async def test_runtime_recovery_requires_this_exact_prepared_query() -> None:
    from unittest.mock import AsyncMock

    from agent_core.adapters.determinism import RandomIdFactory
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from tests.contract.support import RUN_ID

    clock, factory, _, _ = await formation_stack()
    sequence = await user_event(factory, "The workshop is Thursday. Enter through Bay 6.")
    original = memory(statement="The workshop is Thursday.").model_copy(
        update={"subject": "workshop", "scope": "general", "source_event_ids": [sequence]}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
    lookup = Mock()
    lookup.lookup.return_value = None
    runtime = Mock()
    runtime.bind = AsyncMock(return_value=lookup)
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        RandomIdFactory(),
        principal(),
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(update={"semantic_enabled": True}),
        runtime_semantic_queries=runtime,
        source_recovery=BoundedSourceRecovery(principal(), admitted=lambda: True),
    )
    result = await retriever.recall(
        recall_query(text="workshop", current_scope="general").model_copy(
            update={"candidate_selection": "relevance"}
        ),
        session_id=SESSION_ID,
        run_id=RUN_ID,
    )
    assert original.statement in result.rendered
    assert "Bay 6" not in result.rendered, (
        "A handle prepared for another query cannot authorize extra source-recovery work"
    )
