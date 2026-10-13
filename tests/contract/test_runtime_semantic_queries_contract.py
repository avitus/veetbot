"""Run-bound semantic recall uses trusted state on both shared retrieval paths."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse
from agent_core.domain.memory import RecallProfile, RecallQuery, Sensitivity
from agent_core.domain.messages import TextPart, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
from agent_core.memory.retrieval import DeterministicQueryFormer, HybridMemoryRetriever
from agent_core.memory.semantic_runtime import RunSemanticQueries, SemanticQueryAuthority
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.semantic_memory import SemanticQueryLookup
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.memory_fixtures import formation_stack, memory
from tests.contract.semantic_indexing_cases import INDEX_CONFIG, Provider
from tests.contract.semantic_memory_cases import SPACE, VECTOR
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, run, tool_context

AUTHORITY = SemanticQueryAuthority(
    policy_version="policy@1",
    retrieval_policy="retrieval-profile-sha@1",
    surface_id="private",
    sensitivity_floor=Sensitivity.INTERNAL,
)


@dataclass
class State:
    authority: SemanticQueryAuthority | None = AUTHORITY
    enabled: bool = True


async def runtime_stack(
    *, run_changes: dict[str, Any] | None = None
) -> tuple[
    FixedClock, UnitOfWorkFactory, Provider, RunSemanticQueries, HybridMemoryRetriever, State
]:
    clock, factory, _, _ = await formation_stack()
    live = run(status=RunStatus.RUNNING).model_copy(
        update={
            "lease_owner": "worker",
            "lease_epoch": 1,
            "lease_expires_at": NOW + timedelta(minutes=10),
        }
    )
    if run_changes:
        live = live.model_copy(update=run_changes)
    checkpoint = RunCheckpoint(
        run_id=RUN_ID,
        version=1,
        status=RunStatus.RUNNING,
        created_at=NOW,
        conversation=[UserMessage(content=[TextPart(text="Suggest breakfast options")])],
        working_state={"objective": "Plan breakfast"},
    )
    async with factory() as uow:
        await uow.runs.create(live)
        await uow.checkpoints.write(RUN_ID, checkpoint, full=True)
    provider = Provider(factory)
    state = State()
    runtime = RunSemanticQueries(
        factory,
        clock,
        principal(),
        provider,
        INDEX_CONFIG,
        admitted=lambda: state.enabled,
        authority=lambda *_: state.authority,
    )
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8000, 9000)),
        principal(),
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(update={"semantic_enabled": True}),
        reconsolidation_enabled=True,
        reconsolidation_owner_approved_only=True,
        runtime_semantic_queries=runtime,
    )
    return clock, factory, provider, runtime, retriever, state


def query(path: str = "automatic") -> RecallQuery:
    text = "Suggest breakfast options"
    if path == "automatic":
        return DeterministicQueryFormer(principal(), current_scope="project-a").from_text(text)[0]
    return RecallQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        current_scope="project-a",
        text=text,
        candidate_selection="relevance",
        profile=RecallProfile.DEEP,
        budget_tokens=2000,
        max_items=20,
        min_score=0.1,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
    )


@pytest.mark.parametrize("path", ["automatic", "deliberate"])
async def test_shared_runtime_preparation_uses_effective_policy_and_current_run(path: str) -> None:
    _, factory, provider, _, retriever, _ = await runtime_stack()
    original = memory(statement="Dairy causes the user digestive discomfort").model_copy(
        update={"subject": "diet"}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
    prepared = await retriever.prepare_semantic_queries(
        [query(path)],
        session_id=SESSION_ID,
        run_id=RUN_ID,
    )
    assert prepared == 1, "shared preparation must bind and normalize the real run's query"
    if path == "automatic":
        result = await retriever.recall(query(path), session_id=SESSION_ID, run_id=RUN_ID)
        rendered = result.rendered
    else:
        tool_result = await MemorySearchTool(retriever).execute(
            {"text": query(path).text, "scope": "project-a"}, tool_context()
        )
        rendered = str(tool_result.content)
    assert original.statement in rendered, "merge policy must not break the prepared-query key"
    assert len(provider.calls) == 1, "recall must remain a local lookup"


async def prepare(retriever: HybridMemoryRetriever) -> int:
    return await retriever.prepare_semantic_queries([query()], session_id=SESSION_ID, run_id=RUN_ID)


async def bind(runtime: RunSemanticQueries) -> SemanticQueryLookup | None:
    return await runtime.bind(session_id=SESSION_ID, run_id=RUN_ID, surface_id="private")


async def revise_checkpoint(factory: UnitOfWorkFactory, **changes: Any) -> None:
    async with factory() as uow:
        previous = await uow.checkpoints.latest(RUN_ID)
        assert previous is not None
        await uow.checkpoints.write(
            RUN_ID,
            previous.model_copy(update={"version": previous.version + 1, **changes}),
            full=True,
        )


async def test_ordinary_miss_and_missing_authority_do_no_provider_work() -> None:
    _, _, provider, runtime, retriever, state = await runtime_stack()
    assert await bind(runtime) is None
    await retriever.recall(query(), session_id=SESSION_ID, run_id=RUN_ID)
    state.authority = None
    assert await prepare(retriever) == 0
    assert not provider.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"working_state": {"objective": "Plan dinner"}},
        {"conversation": [UserMessage(content=[TextPart(text="Change the plan")])]},
        {"pinned_tool_names": ["memory.search"]},
        {"context_origin_trust": TrustLevel.MEMORY},
    ],
)
async def test_checkpoint_changes_revoke_query_context(changes: dict[str, Any]) -> None:
    _, factory, provider, runtime, retriever, _ = await runtime_stack()
    assert await prepare(retriever) == 1
    handle = await bind(runtime)
    assert handle is not None
    await revise_checkpoint(factory, **changes)
    assert await bind(runtime) is None
    assert handle.lookup(retriever._effective_query(query(), "in_turn")) is None
    assert await prepare(retriever) == 1
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_version", "policy@2"),
        ("retrieval_policy", "new-profile-digest"),
        ("surface_id", "public"),
        ("sensitivity_floor", Sensitivity.SENSITIVE),
    ],
)
async def test_authority_changes_revoke_even_if_later_restored(field: str, value: object) -> None:
    _, _, provider, runtime, retriever, state = await runtime_stack()
    assert await prepare(retriever) == 1
    state.authority = AUTHORITY.model_copy(update={field: value})
    assert await bind(runtime) is None
    state.authority = AUTHORITY
    assert await bind(runtime) is None
    assert await prepare(retriever) == 1
    assert len(provider.calls) == 2


@pytest.mark.parametrize("change", ["cancel", "expired", "terminal"])
async def test_stopped_run_never_prepares_or_reuses(change: str) -> None:
    clock, factory, provider, runtime, retriever, _ = await runtime_stack()
    assert await prepare(retriever) == 1
    if change == "expired":
        clock.advance(timedelta(minutes=10))
    else:
        async with factory() as uow:
            if change == "cancel":
                await uow.runs.request_cancellation(RUN_ID, RunStatus.RUNNING)
            else:
                await uow.runs.transition(RUN_ID, RunStatus.RUNNING, RunStatus.COMPLETED)
    assert await bind(runtime) is None
    assert await prepare(retriever) == 0
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_owner": None},
        {"lease_expires_at": NOW},
        {"principal_scopes": {"unavailable.permission"}},
        {"deadline_at": NOW, "limits": run().limits.model_copy(update={"deadline_at": NOW})},
    ],
)
async def test_unleased_expired_or_unauthorized_run_never_sends(changes: dict[str, Any]) -> None:
    _, _, provider, runtime, retriever, _ = await runtime_stack(run_changes=changes)
    assert await bind(runtime) is None
    assert await prepare(retriever) == 0
    assert not provider.calls


async def test_foreign_session_run_and_surface_never_reuse() -> None:
    _, _, provider, runtime, retriever, _ = await runtime_stack()
    assert await prepare(retriever) == 1
    assert await runtime.bind(session_id=UUID(int=991), run_id=RUN_ID, surface_id="private") is None
    assert (
        await runtime.bind(session_id=SESSION_ID, run_id=UUID(int=992), surface_id="private")
        is None
    )
    assert await runtime.bind(session_id=SESSION_ID, run_id=RUN_ID, surface_id="external") is None
    foreign = query().model_copy(update={"principal_id": "another-owner"})
    assert (
        await retriever.prepare_semantic_queries([foreign], session_id=SESSION_ID, run_id=RUN_ID)
        == 0
    )
    assert len(provider.calls) == 1


async def test_changed_checkpoint_cancels_inflight_request_and_retains_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, provider, runtime, retriever, _ = await runtime_stack()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def block(request: EmbeddingRequest) -> EmbeddingResponse:
        provider.calls.append(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(provider, "embed", block)
    task = asyncio.create_task(prepare(retriever))
    await asyncio.wait_for(entered.wait(), timeout=3)
    await revise_checkpoint(factory, working_state={"objective": "Different task"})
    assert await asyncio.wait_for(task, timeout=3) == 0
    assert cancelled.is_set() and await bind(runtime) is None
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), NOW.date()) == INDEX_CONFIG.price(
            provider.calls[0].maximum_tokens
        )


async def test_outer_cancellation_reaches_provider_and_preserves_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, provider, _, retriever, _ = await runtime_stack()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def block(request: EmbeddingRequest) -> EmbeddingResponse:
        provider.calls.append(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(provider, "embed", block)
    task = asyncio.create_task(prepare(retriever))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), NOW.date()) == INDEX_CONFIG.price(
            provider.calls[0].maximum_tokens
        )


async def test_cache_global_capacity_expiry_and_withdrawal() -> None:
    clock, _, provider, runtime, retriever, state = await runtime_stack()
    for i in range(33):
        queries = [query().model_copy(update={"text": f"meal {i * 4 + j}"}) for j in range(4)]
        assert (
            await retriever.prepare_semantic_queries(queries, session_id=SESSION_ID, run_id=RUN_ID)
            == 4
        )
    assert len(runtime._vectors) == 128
    clock.advance(timedelta(seconds=300))
    assert await bind(runtime) is None
    assert not runtime._vectors
    assert await prepare(retriever) == 1
    state.enabled = False
    assert await bind(runtime) is None and not runtime._vectors
    state.enabled = True
    assert await bind(runtime) is None
    assert len(provider.calls) == 34


async def test_run_contexts_are_isolated_and_revocation_is_local() -> None:
    from tests.contract.support import session

    _, factory, provider, runtime, retriever, _ = await runtime_stack()
    other_run, other_session = UUID(int=701), UUID(int=702)
    async with factory() as uow:
        first = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
        assert checkpoint is not None
        await uow.sessions.create(session().model_copy(update={"id": other_session}))
        await uow.runs.create(
            first.model_copy(update={"id": other_run, "session_id": other_session})
        )
        await uow.checkpoints.write(
            other_run, checkpoint.model_copy(update={"run_id": other_run}), full=True
        )
    assert await prepare(retriever) == 1
    assert (
        await retriever.prepare_semantic_queries(
            [query()], session_id=other_session, run_id=other_run
        )
        == 1
    )
    first_lookup, second_lookup = await asyncio.gather(
        bind(runtime),
        runtime.bind(session_id=other_session, run_id=other_run, surface_id="private"),
    )
    assert first_lookup is not None and second_lookup is not None
    effective = retriever._effective_query(query(), "in_turn")
    assert first_lookup.lookup(effective) is not None
    assert second_lookup.lookup(effective) is not None
    await revise_checkpoint(factory, working_state={"objective": "new task"})
    assert await bind(runtime) is None
    assert first_lookup.lookup(effective) is None
    assert second_lookup.lookup(effective) is not None
    assert len(provider.calls) == 2


async def test_context_capacity_revokes_evicted_handles() -> None:
    from tests.contract.support import session

    _, factory, _, runtime, retriever, _ = await runtime_stack()
    assert await prepare(retriever) == 1
    handle = await bind(runtime)
    assert handle is not None
    async with factory() as uow:
        first = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
        assert checkpoint is not None
    for i in range(32):
        run_id, session_id = UUID(int=10000 + i), UUID(int=20000 + i)
        async with factory() as uow:
            await uow.sessions.create(session().model_copy(update={"id": session_id}))
            await uow.runs.create(first.model_copy(update={"id": run_id, "session_id": session_id}))
            await uow.checkpoints.write(
                run_id, checkpoint.model_copy(update={"run_id": run_id}), full=True
            )
        assert (
            await retriever.prepare_semantic_queries(
                [query()], session_id=session_id, run_id=run_id
            )
            == 1
        )
    assert len(runtime._entries) == 32 and len(runtime._vectors) == 32
    assert handle.lookup(retriever._effective_query(query(), "in_turn")) is None
    assert await bind(runtime) is None


async def test_provider_result_cannot_publish_after_run_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, provider, runtime, retriever, _ = await runtime_stack()
    original = provider.embed

    async def cancel(request: EmbeddingRequest) -> EmbeddingResponse:
        response = await original(request)
        async with factory() as uow:
            await uow.runs.request_cancellation(RUN_ID, RunStatus.RUNNING)
        return response

    monkeypatch.setattr(provider, "embed", cancel)
    assert await prepare(retriever) == 0
    assert await bind(runtime) is None and not runtime._vectors
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), NOW.date()) == INDEX_CONFIG.price(
            provider.calls[0].maximum_tokens
        )


async def test_expired_handle_cannot_revive_when_identical_context_is_prepared_again() -> None:
    clock, _, _, runtime, retriever, _ = await runtime_stack()
    assert await prepare(retriever) == 1
    old = await bind(runtime)
    assert old is not None
    clock.advance(timedelta(seconds=300))
    assert await bind(runtime) is None
    assert await prepare(retriever) == 1
    fresh = await bind(runtime)
    effective = retriever._effective_query(query(), "in_turn")
    assert fresh is not None and fresh.lookup(effective) is not None
    assert old.lookup(effective) is None
    assert fresh.lookup(effective) is not None, "a stale handle cannot evict its replacement"


async def test_optional_binding_failure_preserves_sparse_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, provider, runtime, retriever, _ = await runtime_stack()
    original = memory(statement="Breakfast options include oatmeal").model_copy(
        update={"subject": "breakfast"}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)

    async def unavailable(**kwargs: object) -> SemanticQueryLookup | None:
        raise TimeoutError("unavailable state")

    monkeypatch.setattr(runtime, "bind", unavailable)
    result = await retriever.recall(query(), session_id=SESSION_ID, run_id=RUN_ID)
    assert original.id in {item.belief_id for item in result.items}
    assert not provider.calls


async def test_bound_handle_expires_with_its_run_lease_without_another_state_read() -> None:
    clock, _, _, runtime, retriever, _ = await runtime_stack(
        run_changes={"lease_expires_at": NOW + timedelta(seconds=2)}
    )
    assert await prepare(retriever) == 1
    handle = await bind(runtime)
    assert handle is not None
    clock.advance(timedelta(seconds=2))
    assert handle.lookup(retriever._effective_query(query(), "in_turn")) is None


async def test_authority_can_resolve_current_sources_outside_the_state_transaction() -> None:
    """Privacy floors require current source reads, rather than a cached label."""
    clock, factory, provider, _, _, state = await runtime_stack()
    reads = 0

    async def resolve_authority(*_: Any) -> SemanticQueryAuthority | None:
        nonlocal reads
        async with factory() as uow:
            saved = await uow.checkpoints.latest(RUN_ID)
            assert saved is not None
        reads += 1
        return state.authority

    runtime = RunSemanticQueries(
        factory,
        clock,
        principal(),
        provider,
        INDEX_CONFIG,
        admitted=lambda: state.enabled,
        authority=resolve_authority,
    )
    assert (
        await runtime.prepare([query()], session_id=SESSION_ID, run_id=RUN_ID, surface_id="private")
        == 1
    )
    assert reads >= 3, "Source authority must be refreshed before sending and publishing."
    bound = await bind(runtime)
    assert bound is not None and bound.lookup(query()) is not None
    state.authority = AUTHORITY.model_copy(update={"sensitivity_floor": Sensitivity.RESTRICTED})
    assert await bind(runtime) is None
    assert bound.lookup(query()) is None


async def test_cancel_during_authority_resolution_cannot_return_an_old_handle() -> None:
    clock, factory, provider, _, _, state = await runtime_stack()
    cancel_on_resolve = False

    async def resolve_authority(*_: Any) -> SemanticQueryAuthority | None:
        if cancel_on_resolve:
            async with factory() as uow:
                await uow.runs.request_cancellation(RUN_ID, RunStatus.RUNNING)
        return state.authority

    runtime = RunSemanticQueries(
        factory,
        clock,
        principal(),
        provider,
        INDEX_CONFIG,
        admitted=lambda: state.enabled,
        authority=resolve_authority,
    )
    assert (
        await runtime.prepare([query()], session_id=SESSION_ID, run_id=RUN_ID, surface_id="private")
        == 1
    )
    previous = await bind(runtime)
    assert previous is not None
    cancel_on_resolve = True
    assert await bind(runtime) is None
    assert previous.lookup(query()) is None


async def test_shared_run_deadline_revokes_existing_query_handle() -> None:
    from agent_core.domain.runs import RecallWorkBudget

    clock, factory, provider, runtime, retriever, _ = await runtime_stack()
    async with factory() as uow:
        checkpoint = await uow.checkpoints.latest(RUN_ID)
        assert checkpoint is not None
        await uow.checkpoints.write(
            RUN_ID,
            checkpoint.model_copy(
                update={
                    "version": 2,
                    "recall_work": RecallWorkBudget(
                        searches=1, rounds=1, deadline_at=NOW + timedelta(seconds=10)
                    ),
                }
            ),
            full=True,
        )
    assert (
        await retriever.prepare_semantic_queries([query()], session_id=SESSION_ID, run_id=RUN_ID)
        == 1
    )
    handle = await runtime.bind(session_id=SESSION_ID, run_id=RUN_ID, surface_id="private")
    assert handle is not None
    effective = retriever._effective_query(query(), "in_turn")
    assert handle.lookup(effective) is not None
    clock.advance(timedelta(seconds=11))
    assert handle.lookup(effective) is None, "A retained handle cannot outlive its run deadline"
    assert await runtime.bind(session_id=SESSION_ID, run_id=RUN_ID, surface_id="private") is None
    assert (
        await retriever.prepare_semantic_queries([query()], session_id=SESSION_ID, run_id=RUN_ID)
        == 0
    )
    assert len(provider.calls) == 1
