"""Provider execution and settlement against either transactional store."""

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import cast
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.messages import (
    AssistantMessage,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ModelTurn,
    ModelUsage,
    ResolvedModel,
    StopReason,
    TextPart,
    UserMessage,
)
from agent_core.memory.reconsolidation_execution import ReconsolidationBatchExecutor
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.reconsolidation_admission_cases import model, policy
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.reconsolidation_source_cases import seed
from tests.contract.support import NOW, principal


class TrackedFactory:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory
        self.opened: ContextVar[bool] = ContextVar("execution_uow", default=False)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[Stores]:
        token = self.opened.set(True)
        try:
            async with self.factory() as uow:
                yield uow
        finally:
            self.opened.reset(token)

    def is_open(self) -> bool:
        return self.opened.get()


def reply(request: ModelRequest) -> str:
    message = request.conversation[1]
    assert isinstance(message, UserMessage)
    part = message.content[0]
    assert isinstance(part, TextPart)
    payload = json.loads(part.text)
    if payload["schema_version"] == "reconsolidation-input@1":
        return json.dumps(
            {
                "schema_version": "reconsolidation-proposal@1",
                "batch_id": payload["batch_id"],
                "group_ids": [g["id"] for g in payload["groups"]],
                "operations": [
                    {
                        "id": str(UUID(int=900 + i)),
                        "group_id": g["id"],
                        "kind": "no_change",
                        "inputs": [
                            {"belief_id": s["belief_id"], "content_revision": s["content_revision"]}
                            for s in g["sources"]
                        ],
                        "clauses": [],
                    }
                    for i, g in enumerate(payload["groups"])
                ],
            }
        )
    return json.dumps(
        {
            "schema_version": "reconsolidation-verification@1",
            "batch_id": payload["batch_id"],
            "proposal_digest": payload["proposal_digest"],
            "verdicts": [],
        }
    )


class ExecutionProvider:
    name = "fake"

    def __init__(self, factory: TrackedFactory) -> None:
        self.factory = factory
        self.requests: list[ModelRequest] = []
        self.attempts: list[ModelAttempt] = []
        self.closed_streams = 0

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        assert not self.factory.is_open(), "provider await must be outside every transaction"
        self.requests.append(request.model_copy(deep=True))
        self.attempts.append(attempt)
        try:
            yield ModelCompletedEvent(
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                step_number=attempt.step_number,
                sequence=0,
                turn=ModelTurn(
                    assistant_messages=[
                        AssistantMessage(item_index=0, content=[TextPart(text=reply(request))])
                    ],
                    usage=ModelUsage(
                        input_tokens=100,
                        output_tokens=50,
                        provider=resolved.provider,
                        model=resolved.model,
                    ),
                    stop_reason=StopReason.END_TURN,
                ),
                stop_reason=StopReason.END_TURN,
            )
        finally:
            self.closed_streams += 1

    async def close(self) -> None:
        pass


async def execution_two_calls_settle_without_writing_memories(factory: Factory) -> None:
    tracked = TrackedFactory(factory)
    async with tracked() as uow:
        job, group = await seed(uow)
        originals = [await uow.memories.get(s.belief_id, principal()) for s in group.sources]
    provider = ExecutionProvider(tracked)
    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason == "reviewed", (
        "a bounded two-call batch must yield a local-validation review"
    )
    assert result.review is not None and result.review.candidates[0].requires_local_validation
    assert result.prepared is not None
    assert len(provider.requests) == provider.closed_streams == len(result.calls) == 2
    assert len({a.attempt_id for a in provider.attempts}) == 2
    for request, call in zip(provider.requests, result.calls, strict=True):
        assert request.maximum_provider_attempts == 1
        assert (
            call.spend.request_digest
            == hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        )
        assert call.spend.state == "settled" and call.spend.charged_usd == Decimal("0.0002")
    async with tracked() as uow:
        current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        assert current.requests == 2 and current.slice_spent == Decimal("0.0004")
        for original in originals:
            assert await uow.memories.get(original.id, principal()) == original
    again = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert again.reason != "reviewed" and len(provider.requests) == 2


EXECUTION_SCENARIOS: list[Callable[[Factory], Awaitable[None]]] = [
    execution_two_calls_settle_without_writing_memories,
]


async def execution_cancellation_keeps_committed_reservation(factory: Factory) -> None:
    import asyncio

    import pytest

    tracked = TrackedFactory(factory)
    async with tracked() as uow:
        job, group = await seed(uow)
    entered, closed = asyncio.Event(), asyncio.Event()

    class Hanging(ExecutionProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            assert not tracked.is_open()
            async with tracked() as uow:
                current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
                assert current.requests == 1 and current.slice_spent > 0
            entered.set()
            try:
                await asyncio.Event().wait()
                yield cast(ModelEvent, None)
            finally:
                closed.set()

    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    task = asyncio.create_task(
        executor.run(
            job, (group.id,), model=model(), provider=Hanging(tracked), batch_id=UUID(int=90)
        )
    )
    async with asyncio.timeout(5):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    async with tracked() as uow:
        current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        assert current.requests == 1 and current.slice_spent > 0
        await uow.reconsolidation.finish_group(principal(), job.lease_token, group.id, "retry", NOW)
        await uow.reconsolidation.release(principal(), job.lease_token, NOW)


async def execution_source_change_prevents_verification(factory: Factory) -> None:
    tracked = TrackedFactory(factory)
    async with tracked() as uow:
        job, group = await seed(uow)

    class ChangingSource(ExecutionProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            async for event in super().stream(request, resolved, attempt):
                async with tracked() as uow:
                    source = await uow.memories.get(group.sources[0].belief_id, principal())
                    await uow.memories.reinforce(source.model_copy(update={"confidence": 0.8}))
                yield event

    provider = ChangingSource(tracked)
    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason == "deferred" and result.review is None
    assert len(provider.requests) == len(result.calls) == 1
    assert result.calls[0].spend.state == "settled"


async def execution_failed_settlement_stops_then_recovers(
    factory: Factory, *, fail_on_exit: bool = False
) -> None:
    from datetime import timedelta
    from typing import Any

    import pytest

    from agent_core.domain.errors import ConflictError
    from agent_core.ports.reconsolidation import ReconsolidationStore

    tracked = TrackedFactory(factory)
    async with tracked() as uow:
        job, group = await seed(uow)

    class FailingSettlement:
        def __init__(self, store: ReconsolidationStore) -> None:
            self.store = store
            self.settled = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.store, name)

        async def settle(self, *args: Any, **kwargs: Any) -> Any:
            value = await self.store.settle(*args, **kwargs)
            self.settled = True
            if not fail_on_exit:
                raise RuntimeError("simulated failure after settlement before commit")
            return value

    @asynccontextmanager
    async def failing() -> AsyncIterator[Stores]:
        async with tracked() as uow:
            proxy = FailingSettlement(uow.reconsolidation)
            yield Stores(
                uow.memories,
                cast(ReconsolidationStore, proxy),
                uow.events,
                uow.people,
            )
            if fail_on_exit and proxy.settled:
                raise RuntimeError("simulated failed settlement commit")

    failing_factory = TrackedFactory(failing)
    provider = ExecutionProvider(failing_factory)
    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, failing_factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason == "settlement_failed" and result.review is None
    assert len(provider.requests) == len(result.calls) == 1
    reservation = result.calls[0].spend
    assert reservation.state == "reserved" and reservation.charged_usd == reservation.maximum_usd
    assert reservation.call_audit is not None and reservation.call_audit.completion is None
    async with tracked() as uow:
        current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        assert current.slice_spent == reservation.maximum_usd
    later = NOW + timedelta(seconds=181)
    async with tracked() as uow:
        recovered = await uow.reconsolidation.claim_due(principal(), later, "recovery")
        assert recovered is not None and recovered.lease_token != job.lease_token
        assert (
            await uow.reconsolidation.claim_group(principal(), recovered.lease_token, later)
            is not None
        )
    async with tracked() as uow:
        saved = await uow.reconsolidation.get_spend(principal(), reservation.id)
        assert saved.call_audit is not None and saved.call_audit.completion is not None
        assert saved.call_audit.completion.reason == "recovered_unknown"
        assert saved.call_audit.completion.tokens is None and saved.call_audit.decision is None
        with pytest.raises(ConflictError):
            await uow.reconsolidation.settle(
                principal(), job.lease_token, reservation.id, Decimal(0), later
            )


EXECUTION_SCENARIOS += [
    execution_cancellation_keeps_committed_reservation,
    execution_source_change_prevents_verification,
    execution_failed_settlement_stops_then_recovers,
]


async def execution_failed_settlement_commit_preserves_full_charge(factory: Factory) -> None:
    await execution_failed_settlement_stops_then_recovers(factory, fail_on_exit=True)


EXECUTION_SCENARIOS.append(execution_failed_settlement_commit_preserves_full_charge)
