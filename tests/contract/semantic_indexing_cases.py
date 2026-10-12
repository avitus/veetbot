"""Background admission and evidence fences, exercised against both repositories."""

from decimal import Decimal

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.unit_of_work import _unit_of_work_is_open
from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest, EmbeddingResponse
from agent_core.domain.memory import Sensitivity
from agent_core.memory.reconsolidation_policy import POLICY_VERSION
from agent_core.memory.semantic_indexing import SemanticIndexingPass
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.semantic_work_cases import CONFIG
from tests.contract.support import NOW, principal

INDEX_CONFIG = CONFIG.model_copy(update={"egress_revision": POLICY_VERSION})


class Provider:
    def __init__(
        self, factory: UnitOfWorkFactory, *, fail: bool = False, correct: bool = False
    ) -> None:
        self.factory, self.fail, self.correct = factory, fail, correct
        self.calls: list[EmbeddingRequest] = []

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        assert not _unit_of_work_is_open(), "provider sends must happen after admission commits"
        self.calls.append(request)
        async with self.factory() as uow:
            assert await uow.semantic_work.spent(principal(), NOW.date()) > 0
            if self.correct:
                value = await uow.memories.get(memory().id, principal())
                await uow.memories.reinforce(
                    value.model_copy(update={"statement": "User now prefers tea"})
                )
        if self.fail:
            raise EmbeddingError("unavailable")
        return EmbeddingResponse(
            request_digest=request.digest,
            vectors=tuple((1.0, 0.0, 0.0) for _ in request.texts),
            input_tokens=2,
        )


def worker(
    factory: UnitOfWorkFactory, provider: Provider, *, enabled: bool = True
) -> SemanticIndexingPass:
    return SemanticIndexingPass(
        factory, FixedClock(NOW), principal(), provider, INDEX_CONFIG, admitted=lambda: enabled
    )


async def admitted_indexing(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(
            memory(statement="User prefers quiet workspaces")
        )
    provider = Provider(factory)
    result = await worker(factory, provider).run_slice()
    assert result.indexed == 1 and len(provider.calls) == 1, (
        "eligible originals must be indexed after durable admission"
    )
    async with factory() as uow:
        hits = await uow.semantic_memory.search(
            recall_query(text="place to concentrate"), CONFIG.space, (1.0, 0.0, 0.0), limit=1
        )
        assert [h.record.id for h in hits] == [original.id]
        assert await uow.semantic_work.spent(principal(), NOW.date()) == INDEX_CONFIG.price(2)
    assert (await worker(factory, provider).run_slice()).indexed == 0
    assert len(provider.calls) == 1, "completed inventory must not resend unchanged sources"
    async with factory() as uow:
        await uow.memories.upsert_belief(
            memory(belief_id=502, statement="User prefers morning meetings").model_copy(
                update={"store_position": 2}
            )
        )
    assert (await worker(factory, provider).run_slice()).indexed == 1
    assert len(provider.calls) == 2 and len(provider.calls[-1].texts) == 1


async def denied_sources_are_not_serialized(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory(statement="User prefers quiet workspaces"))
        await uow.memories.upsert_belief(
            memory(belief_id=502, statement="User has a private medical diagnosis").model_copy(
                update={"store_position": 2}
            )
        )
        await uow.memories.upsert_belief(
            memory(belief_id=503).model_copy(
                update={"sensitivity": Sensitivity.RESTRICTED, "store_position": 3}
            )
        )
    provider = Provider(factory)
    result = await worker(factory, provider).run_slice()
    assert result.indexed == 1 and result.deferred == 2
    assert all("diagnosis" not in text for call in provider.calls for text in call.texts)
    assert len(provider.calls[0].texts) == 1


async def source_change_during_send(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    provider = Provider(factory, correct=True)
    result = await worker(factory, provider).run_slice()
    assert len(provider.calls) == 1 and result.indexed == 0
    async with factory() as uow:
        assert (
            await uow.semantic_memory.search(recall_query(), CONFIG.space, (1.0, 0.0, 0.0), limit=1)
            == []
        )
        assert await uow.semantic_work.spent(principal(), NOW.date()) == INDEX_CONFIG.price(2)


async def failed_requests_retain_spend(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    provider = Provider(factory, fail=True)
    for _ in range(3):
        result = await worker(factory, provider).run_slice()
        assert result.reason == "provider_failed"
    await worker(factory, provider).run_slice()
    assert len(provider.calls) == 3, (
        "three uncertain sends must not turn into an infinite retry loop"
    )
    async with factory() as uow:
        expected = sum((INDEX_CONFIG.price(r.maximum_tokens) for r in provider.calls), Decimal(0))
        assert await uow.semantic_work.spent(principal(), NOW.date()) == expected


async def disabled_and_residency_unknown(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    provider = Provider(factory)
    assert (await worker(factory, provider, enabled=False).run_slice()).reason == "disabled"
    config = INDEX_CONFIG.model_copy(update={"residency_provider": "elsewhere"})
    result = await SemanticIndexingPass(
        factory, FixedClock(NOW), principal(), provider, config, admitted=lambda: True
    ).run_slice()
    assert result.deferred == 1 and provider.calls == []


SCENARIOS = [
    admitted_indexing,
    denied_sources_are_not_serialized,
    source_change_during_send,
    failed_requests_retain_spend,
    disabled_and_residency_unknown,
]


async def prose_credentials_are_not_exported(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory(statement="The API key is synthetic-example-value"))
    provider = Provider(factory)
    result = await worker(factory, provider).run_slice()
    assert result.deferred == 1 and provider.calls == [], (
        "prose credentials must fail before serialization"
    )


SCENARIOS.append(prose_credentials_are_not_exported)


async def _interrupted(factory: UnitOfWorkFactory, *, withdraw: bool) -> None:
    import asyncio

    import pytest

    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Blocking(Provider):
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            self.calls.append(request)
            assert not _unit_of_work_is_open()
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
            raise AssertionError("unreachable")

    provider = Blocking(factory)
    enabled = [True]
    engine = SemanticIndexingPass(
        factory, FixedClock(NOW), principal(), provider, INDEX_CONFIG, admitted=lambda: enabled[0]
    )
    task = asyncio.create_task(engine.run_slice())
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert (await engine.run_slice()).reason == "not_due", "only one owner worker may send"
        if withdraw:
            enabled[0] = False
            assert (await asyncio.wait_for(task, 3)).reason == "provider_failed"
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped.is_set() and len(provider.calls) == 1
        async with factory() as uow:
            assert await uow.semantic_work.spent(principal(), NOW.date()) == INDEX_CONFIG.price(
                provider.calls[0].maximum_tokens
            )
            assert (
                await uow.semantic_memory.search(
                    recall_query(), CONFIG.space, (1.0, 0.0, 0.0), limit=1
                )
                == []
            )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def cancellation_retains_charge(factory: UnitOfWorkFactory) -> None:
    await _interrupted(factory, withdraw=False)


async def withdrawal_cancels_send(factory: UnitOfWorkFactory) -> None:
    await _interrupted(factory, withdraw=True)


async def old_inventory_is_not_starved(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        for i in range(40):
            await uow.memories.upsert_belief(
                memory(belief_id=1000 + i, statement=f"User follows project {i}").model_copy(
                    update={"store_position": 1000 + i}
                )
            )
    provider = Provider(factory)
    assert (await worker(factory, provider).run_slice()).indexed == 16
    async with factory() as uow:
        for i in range(40, 56):
            await uow.memories.upsert_belief(
                memory(belief_id=1000 + i, statement=f"User follows project {i}").model_copy(
                    update={"store_position": 1000 + i}
                )
            )
    assert (await worker(factory, provider).run_slice()).indexed == 16
    assert (await worker(factory, provider).run_slice()).indexed == 8
    async with factory() as uow:
        hits = await uow.semantic_memory.search(
            recall_query(text="project"), CONFIG.space, (1.0, 0.0, 0.0), limit=64
        )
        assert len(hits) == 40
    assert len(provider.calls) == 3


SCENARIOS.extend(
    [cancellation_retains_charge, withdrawal_cancels_send, old_inventory_is_not_starved]
)
