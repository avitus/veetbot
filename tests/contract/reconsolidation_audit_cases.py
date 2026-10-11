"""Durable content-free provider audit across transactional store adapters."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.reconsolidation import (
    ReconsolidationGroup,
    ReconsolidationJob,
    ReconsolidationSpend,
    StageDecision,
)
from agent_core.domain.reconsolidation_inputs import PreparedProposalRequest
from agent_core.memory.reconsolidation_admission import ReconsolidationRequestAdmission
from agent_core.memory.reconsolidation_execution import ReconsolidationBatchExecutor
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.reconsolidation_admission_cases import model, policy
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_execution_cases import ExecutionProvider, TrackedFactory
from tests.contract.reconsolidation_input_cases import named
from tests.contract.reconsolidation_source_cases import seed
from tests.contract.support import NOW, principal


async def provider_audit_survives_reopen(factory: Factory) -> None:
    tracked = TrackedFactory(factory)
    async with tracked() as uow:
        job, group = await seed(uow)
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
    assert result.reason == "reviewed" and len(result.calls) == 2
    assert result.calls[0].spend.model_dump().get("call_audit") is not None, (
        "admission and settlement need a durable content-free call audit"
    )
    for stage, call in zip(("proposal", "verification"), result.calls, strict=True):
        async with tracked() as uow:
            saved = await uow.reconsolidation.get_spend(principal(), call.spend.id)
        assert saved == call.spend
        audit = saved.call_audit
        assert audit is not None
        assert audit.admission.stage == stage and audit.admission.group_ids == (group.id,)
        assert audit.admission.batch_id == UUID(int=90)
        assert audit.admission.provider == model().provider
        assert audit.admission.model == model().model
        assert audit.admission.admitted_at == NOW
        assert audit.completion is not None and audit.completion.reason == "completed"
        assert audit.completion.finished_at == NOW
        assert audit.completion.elapsed_ms is not None and audit.completion.elapsed_ms >= 0
        assert audit.completion.tokens is not None
        assert audit.completion.tokens.input_tokens == 100
        assert audit.completion.tokens.output_tokens == 50
        assert audit.completion.tokens.cached_input_tokens == 0
        assert audit.completion.tokens.reasoning_tokens is None
        assert audit.decision == StageDecision(
            status="prepared" if stage == "proposal" else "reviewed", validated=1
        )
        assert saved.charged_usd == Decimal("0.0002")


async def audit_crash_recovery_preserves_admission(factory: Factory) -> None:
    admission = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    async with factory() as uow:
        job, group = await seed(uow)
    _, spend = await admission.proposal(
        job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90)
    )
    assert spend is not None and spend.call_audit is not None
    assert spend.call_audit.completion is None and spend.call_audit.decision is None
    later = NOW + timedelta(seconds=181)
    async with factory() as uow:
        assert await uow.reconsolidation.claim_due(principal(), later, "recover") is not None
    async with factory() as uow:
        recovered = await uow.reconsolidation.get_spend(principal(), spend.id)
    assert recovered.state == "unknown" and recovered.charged_usd == spend.maximum_usd
    audit = recovered.call_audit
    assert audit is not None and audit.admission == spend.call_audit.admission
    assert audit.completion is not None and audit.completion.reason == "recovered_unknown"
    assert audit.completion.tokens is None and audit.completion.elapsed_ms is None
    assert audit.completion.finished_at == later and audit.decision is None


AUDIT_SCENARIOS: list[Callable[[Factory], Awaitable[None]]] = [
    provider_audit_survives_reopen,
    audit_crash_recovery_preserves_admission,
]


async def audited_reservation(
    factory: Factory,
) -> tuple[ReconsolidationJob, ReconsolidationGroup, PreparedProposalRequest, ReconsolidationSpend]:
    admission = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    async with factory() as uow:
        job, group = await seed(uow)
    prepared, spend = await admission.proposal(
        job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90)
    )
    assert prepared.prepared is not None and spend is not None
    return job, group, prepared.prepared, spend


async def audit_replay_is_immutable(factory: Factory) -> None:
    import pytest

    from agent_core.domain.errors import ConflictError
    from agent_core.domain.reconsolidation import CallCompletion

    job, _, _, spend = await audited_reservation(factory)
    completion = CallCompletion(reason="completed", finished_at=NOW, elapsed_ms=10)
    decision = StageDecision(status="prepared", validated=1)
    async with factory() as uow:
        settled = await uow.reconsolidation.settle(
            principal(), job.lease_token, spend.id, Decimal("0.001"), NOW, completion=completion
        )
        decided = await uow.reconsolidation.record_stage_decision(
            principal(), job.lease_token, spend.id, decision, NOW
        )
    async with factory() as uow:
        assert (
            await uow.reconsolidation.settle(
                principal(), job.lease_token, spend.id, Decimal("0.001"), NOW, completion=completion
            )
            == decided
        )
        assert (
            await uow.reconsolidation.record_stage_decision(
                principal(), job.lease_token, spend.id, decision, NOW
            )
            == decided
        )
    for changed in (
        completion.model_copy(update={"reason": "timeout"}),
        completion.model_copy(update={"elapsed_ms": 11}),
    ):
        async with factory() as uow:
            with pytest.raises(ConflictError, match="completion already recorded"):
                await uow.reconsolidation.settle(
                    principal(),
                    job.lease_token,
                    spend.id,
                    Decimal("0.001"),
                    NOW,
                    completion=changed,
                )
    async with factory() as uow:
        with pytest.raises(ConflictError, match="decision already recorded"):
            await uow.reconsolidation.record_stage_decision(
                principal(), job.lease_token, spend.id, StageDecision(status="deferred"), NOW
            )
        assert (await uow.reconsolidation.get_spend(principal(), spend.id)) == decided
        assert (
            await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        ).slice_spent == settled.charged_usd


async def audit_owner_and_lease_isolation(factory: Factory) -> None:
    import pytest

    from agent_core.domain.errors import ConflictError, NotFoundError
    from agent_core.domain.reconsolidation import CallCompletion

    job, _, _, spend = await audited_reservation(factory)
    for other in (
        principal().model_copy(update={"tenant_id": "another-tenant"}),
        principal().model_copy(update={"principal_id": "another-principal"}),
    ):
        async with factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.reconsolidation.get_spend(other, spend.id)
            with pytest.raises((NotFoundError, ConflictError)):
                await uow.reconsolidation.record_stage_decision(
                    other, job.lease_token, spend.id, StageDecision(status="prepared"), NOW
                )
    later = NOW + timedelta(seconds=181)
    async with factory() as uow:
        await uow.reconsolidation.claim_due(principal(), later, "recover")
    async with factory() as uow:
        with pytest.raises(ConflictError):
            await uow.reconsolidation.settle(
                principal(),
                job.lease_token,
                spend.id,
                Decimal(0),
                later,
                completion=CallCompletion(reason="completed", finished_at=later),
            )
        with pytest.raises(ConflictError):
            await uow.reconsolidation.record_stage_decision(
                principal(), job.lease_token, spend.id, StageDecision(status="prepared"), later
            )
        saved = await uow.reconsolidation.get_spend(principal(), spend.id)
        assert saved.state == "unknown" and saved.charged_usd == saved.maximum_usd


async def audit_transaction_rollback(factory: Factory) -> None:
    import pytest

    from agent_core.domain.reconsolidation import CallCompletion

    job, _, _, spend = await audited_reservation(factory)
    completion = CallCompletion(reason="completed", finished_at=NOW)
    with pytest.raises(RuntimeError, match="rollback"):
        async with factory() as uow:
            await uow.reconsolidation.settle(
                principal(), job.lease_token, spend.id, Decimal("0.001"), NOW, completion=completion
            )
            raise RuntimeError("rollback")
    async with factory() as uow:
        assert await uow.reconsolidation.get_spend(principal(), spend.id) == spend
        settled = await uow.reconsolidation.settle(
            principal(), job.lease_token, spend.id, Decimal("0.001"), NOW, completion=completion
        )
    with pytest.raises(RuntimeError, match="rollback"):
        async with factory() as uow:
            await uow.reconsolidation.record_stage_decision(
                principal(), job.lease_token, spend.id, StageDecision(status="prepared"), NOW
            )
            raise RuntimeError("rollback")
    async with factory() as uow:
        assert await uow.reconsolidation.get_spend(principal(), spend.id) == settled


async def audit_refuses_uncompleted_or_wrong_stage(factory: Factory) -> None:
    import pytest

    from agent_core.domain.errors import ConflictError
    from agent_core.domain.reconsolidation import CallCompletion

    job, _, _, spend = await audited_reservation(factory)
    async with factory() as uow:
        with pytest.raises(ConflictError, match="completed call"):
            await uow.reconsolidation.record_stage_decision(
                principal(), job.lease_token, spend.id, StageDecision(status="prepared"), NOW
            )
        await uow.reconsolidation.settle(
            principal(),
            job.lease_token,
            spend.id,
            None,
            NOW,
            completion=CallCompletion(reason="completed", finished_at=NOW),
        )
        with pytest.raises(ConflictError, match="admitted stage"):
            await uow.reconsolidation.record_stage_decision(
                principal(), job.lease_token, spend.id, StageDecision(status="reviewed"), NOW
            )


AUDIT_SCENARIOS += [
    audit_replay_is_immutable,
    audit_owner_and_lease_isolation,
    audit_transaction_rollback,
    audit_refuses_uncompleted_or_wrong_stage,
]


async def audit_provider_outcomes(factory: Factory, *, mode: str) -> None:
    import json
    from collections.abc import AsyncIterator

    from agent_core.domain.messages import (
        AssistantMessage,
        ModelAttempt,
        ModelCompletedEvent,
        ModelEvent,
        ModelRequest,
        ResolvedModel,
        TextPart,
        UserMessage,
    )
    from tests.contract.memory_fixtures import memory

    tracked = TrackedFactory(factory)
    async with tracked() as uow:
        job, group = await seed(uow)
    private = "PRIVATE model response and exception text"

    class Changed(ExecutionProvider):
        async def stream(
            self,
            request: ModelRequest,
            resolved: ResolvedModel,
            attempt: ModelAttempt,
        ) -> AsyncIterator[ModelEvent]:
            if mode == "unavailable":
                raise RuntimeError(private)
            if mode == "timeout":
                raise TimeoutError(private)
            async for event in super().stream(request, resolved, attempt):
                assert isinstance(event, ModelCompletedEvent)
                stage = "bad_proposal" if attempt.step_number == 1 else "bad_verification"
                if mode == stage:
                    event.turn.assistant_messages = [
                        AssistantMessage(item_index=0, content=[TextPart(text=private)])
                    ]
                elif mode == "unknown_usage":
                    event.turn.usage = event.turn.usage.model_copy(update={"provider": private})
                elif mode == "verified_rejection":
                    message = request.conversation[1]
                    assert isinstance(message, UserMessage)
                    part = message.content[0]
                    assert isinstance(part, TextPart)
                    payload = json.loads(part.text)
                    output = event.turn.assistant_messages[0].content[0]
                    assert isinstance(output, TextPart)
                    response = json.loads(output.text)
                    if "operations" in response:
                        operation = response["operations"][0]
                        operation["kind"] = "merge_equivalent"
                        operation["clauses"] = [
                            {
                                "text": memory().statement,
                                "support": [
                                    {
                                        "belief_id": source["belief_id"],
                                        "excerpt_id": source["excerpt_ids"][0],
                                    }
                                    for source in payload["groups"][0]["sources"]
                                ],
                            }
                        ]
                    else:
                        response["verdicts"] = [
                            {
                                "operation_id": payload["operations"][0]["id"],
                                "clause_index": 0,
                                "status": "unsupported",
                                "reason": "contradicted",
                            }
                        ]
                    event.turn.assistant_messages = [
                        AssistantMessage(
                            item_index=0, content=[TextPart(text=json.dumps(response))]
                        )
                    ]
                elif mode == "source_change":
                    async with tracked() as uow:
                        source = await uow.memories.get(group.sources[0].belief_id, principal())
                        await uow.memories.reinforce(source.model_copy(update={"confidence": 0.8}))
                yield event

    result = await ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    ).run(job, (group.id,), model=model(), provider=Changed(tracked), batch_id=UUID(int=90))
    assert result.calls
    for call in result.calls:
        async with tracked() as uow:
            saved = await uow.reconsolidation.get_spend(principal(), call.spend.id)
        assert saved == call.spend and private not in saved.model_dump_json()
    saved = result.calls[-1].spend
    audit = saved.call_audit
    assert audit is not None and audit.completion is not None
    if mode.startswith("bad_"):
        assert result.reason == "invalid_response"
        assert audit.completion.reason == "completed"
        assert audit.decision == StageDecision(status="invalid_response")
        assert saved.state == "settled"
    elif mode == "source_change":
        assert result.reason == "deferred"
        assert audit.decision is not None and audit.decision.status == "deferred"
    elif mode == "verified_rejection":
        assert result.reason == "reviewed"
        assert audit.decision == StageDecision(status="reviewed", rejected=1)
    elif mode == "unknown_usage":
        assert result.reason == "reviewed" and audit.decision is not None
        assert saved.state == "unknown" and saved.charged_usd == saved.maximum_usd
        assert audit.completion.tokens is None
    else:
        assert audit.completion.reason == mode and audit.decision is None
        assert saved.state == "unknown" and saved.charged_usd == saved.maximum_usd
        assert audit.completion.tokens is None


async def audit_cancelled_call(factory: Factory) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    import pytest

    from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest, ResolvedModel
    from agent_core.domain.reconsolidation_execution import ReconsolidationCall

    job, _, prepared, spend = await audited_reservation(factory)
    entered = asyncio.Event()
    tracked = TrackedFactory(factory)

    class Hanging(ExecutionProvider):
        async def stream(
            self,
            request: ModelRequest,
            resolved: ResolvedModel,
            attempt: ModelAttempt,
        ) -> AsyncIterator[ModelEvent]:
            entered.set()
            await asyncio.Event().wait()
            yield cast(ModelEvent, None)

    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    calls: list[ReconsolidationCall] = []
    task = asyncio.create_task(
        executor._call(prepared, spend, model(), Hanging(tracked), "proposal", job, calls)
    )
    async with asyncio.timeout(5):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with factory() as uow:
        saved = await uow.reconsolidation.get_spend(principal(), spend.id)
    assert saved.state == "unknown" and saved.charged_usd == spend.maximum_usd
    assert saved.call_audit is not None and saved.call_audit.completion is not None
    assert saved.call_audit.completion.reason == "cancelled"
    assert saved.call_audit.completion.tokens is None and saved.call_audit.decision is None
    assert calls[0].spend == saved


async def audit_decision_receipt_waits_for_commit(factory: Factory) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from typing import Any

    from agent_core.ports.reconsolidation import ReconsolidationStore
    from tests.contract.reconsolidation_cases import Stores

    async with factory() as uow:
        job, group = await seed(uow)

    class Proxy:
        def __init__(self, store: ReconsolidationStore) -> None:
            self.store = store
            self.changed = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.store, name)

        async def record_stage_decision(self, *args: Any, **kwargs: Any) -> Any:
            value = await self.store.record_stage_decision(*args, **kwargs)
            self.changed = True
            return value

    @asynccontextmanager
    async def failing() -> AsyncIterator[Stores]:
        async with factory() as uow:
            proxy = Proxy(uow.reconsolidation)
            yield Stores(uow.memories, cast(ReconsolidationStore, proxy), uow.events, uow.people)
            if proxy.changed:
                raise RuntimeError("failed audit commit")

    tracked = TrackedFactory(failing)
    provider = ExecutionProvider(tracked)
    result = await ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    ).run(job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90))
    assert result.reason == "unavailable" and len(provider.requests) == 1
    async with factory() as uow:
        saved = await uow.reconsolidation.get_spend(principal(), result.calls[0].spend.id)
    assert saved == result.calls[0].spend
    assert saved.call_audit is not None and saved.call_audit.decision is None
    assert saved.call_audit.completion is not None and saved.state == "settled"


AUDIT_SCENARIOS += [
    named(audit_provider_outcomes, "audit_" + mode, mode=mode)
    for mode in (
        "bad_proposal",
        "bad_verification",
        "unknown_usage",
        "verified_rejection",
        "unavailable",
        "timeout",
        "source_change",
    )
]
AUDIT_SCENARIOS += [audit_cancelled_call, audit_decision_receipt_waits_for_commit]


async def audit_rejects_unclaimed_group(factory: Factory) -> None:
    import pytest

    from agent_core.domain.errors import ConflictError

    job, _, _, spend = await audited_reservation(factory)
    assert spend.call_audit is not None
    changed = spend.call_audit.admission.model_copy(update={"group_ids": (UUID(int=99999),)})
    async with factory() as uow:
        with pytest.raises(ConflictError, match="audit group"):
            await uow.reconsolidation.reserve(
                principal(), job.lease_token, "b" * 64, Decimal("0.001"), NOW, admission=changed
            )
        assert (await uow.reconsolidation.renew(principal(), job.lease_token, NOW)).requests == 1


AUDIT_SCENARIOS.append(audit_rejects_unclaimed_group)


async def audit_usage_is_pinned_before_settlement(factory: Factory) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from agent_core.domain.messages import (
        ModelAttempt,
        ModelCompletedEvent,
        ModelEvent,
        ModelRequest,
        ModelUsage,
        ResolvedModel,
    )
    from tests.contract.reconsolidation_cases import Stores

    usage: ModelUsage | None = None

    @asynccontextmanager
    async def modifying() -> AsyncIterator[Stores]:
        nonlocal usage
        if usage is not None:
            usage.input_tokens = 500
            usage.output_tokens = 300
            usage = None
        async with factory() as uow:
            yield uow

    tracked = TrackedFactory(modifying)
    async with tracked() as uow:
        job, group = await seed(uow)

    class Mutable(ExecutionProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            nonlocal usage
            async for event in super().stream(request, resolved, attempt):
                assert isinstance(event, ModelCompletedEvent)
                usage = event.turn.usage
                yield event

    result = await ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    ).run(job, (group.id,), model=model(), provider=Mutable(tracked), batch_id=UUID(int=90))
    assert result.reason == "reviewed"
    for call in result.calls:
        async with factory() as uow:
            saved = await uow.reconsolidation.get_spend(principal(), call.spend.id)
        assert saved.charged_usd == Decimal("0.0002")
        assert saved.call_audit is not None and saved.call_audit.completion is not None
        tokens = saved.call_audit.completion.tokens
        assert tokens is not None and tokens.input_tokens == 100 and tokens.output_tokens == 50, (
            "durable usage must match the snapshot used to settle the bill"
        )
        assert call.input_tokens == 100 and call.output_tokens == 50


AUDIT_SCENARIOS.append(audit_usage_is_pinned_before_settlement)


async def audit_duration_uses_injected_clock(factory: Factory) -> None:
    from collections.abc import AsyncIterator

    from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest, ResolvedModel

    tracked = TrackedFactory(factory)
    clock = FixedClock(NOW)
    async with tracked() as uow:
        job, group = await seed(uow)

    class Delayed(ExecutionProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            async for event in super().stream(request, resolved, attempt):
                clock.advance(timedelta(milliseconds=250))
                yield event

    result = await ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        clock,
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    ).run(job, (group.id,), model=model(), provider=Delayed(tracked), batch_id=UUID(int=90))
    assert result.reason == "reviewed"
    for index, call in enumerate(result.calls, start=1):
        async with factory() as uow:
            saved = await uow.reconsolidation.get_spend(principal(), call.spend.id)
        assert saved.call_audit is not None and saved.call_audit.completion is not None
        completion = saved.call_audit.completion
        assert completion.elapsed_ms == 250, "audit duration must come from the injected clock"
        assert completion.finished_at == NOW + timedelta(milliseconds=250 * index)


AUDIT_SCENARIOS.append(audit_duration_uses_injected_clock)
