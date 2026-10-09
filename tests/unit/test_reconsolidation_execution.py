"""Failure, cancellation, binding and budget boundaries of M32 provider execution."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.messages import (
    AssistantMessage,
    ImageReferencePart,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelRequest,
    ModelTransientError,
    ModelUsage,
    ResolvedModel,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ToolCallDeltaEvent,
    UsageEvent,
)
from agent_core.memory.reconsolidation_execution import ReconsolidationBatchExecutor
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.reconsolidation_admission_cases import model, policy
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_execution_cases import ExecutionProvider, TrackedFactory
from tests.contract.reconsolidation_source_cases import seed
from tests.contract.support import NOW, memory_uow_factory, principal


class ChangedProvider(ExecutionProvider):
    def __init__(
        self, factory: TrackedFactory, change: Callable[[ModelCompletedEvent], list[ModelEvent]]
    ) -> None:
        super().__init__(factory)
        self.change = change

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        async for event in super().stream(request, resolved, attempt):
            assert isinstance(event, ModelCompletedEvent)
            for changed in self.change(event):
                yield changed


async def setup() -> tuple[Any, ...]:
    _, factory = await memory_uow_factory()
    tracked = TrackedFactory(cast(Factory, factory))
    async with tracked() as uow:
        job, group = await seed(uow)
    clock = FixedClock(NOW)
    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        clock,
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    return tracked, clock, job, group, executor


def changed_text(event: ModelCompletedEvent, value: str) -> ModelCompletedEvent:
    return event.model_copy(
        update={
            "turn": event.turn.model_copy(
                update={
                    "assistant_messages": [
                        AssistantMessage(item_index=0, content=[TextPart(text=value)])
                    ],
                }
            )
        }
    )


def corrupt(event: ModelCompletedEvent, mode: str) -> list[ModelEvent]:
    identity = event.model_dump(include={"attempt_id", "run_id", "step_number", "sequence"})
    if mode == "failed":
        return [
            ModelFailedEvent(
                **identity,
                error=ModelTransientError(
                    provider="fake",
                    model="test-model",
                    attempt_id=event.attempt_id,
                    message="private provider diagnostic",
                    stream_had_output=False,
                ),
            )
        ]
    if mode == "exception":
        raise RuntimeError("private provider diagnostic")
    if mode == "missing_terminal":
        return []
    if mode == "extra_terminal":
        return [event, event.model_copy(update={"sequence": 1})]
    if mode in {"attempt_id", "run_id"}:
        return [event.model_copy(update={mode: UUID(int=12345)})]
    if mode in {"sequence", "step_number", "internal_retry_count"}:
        return [event.model_copy(update={mode: 99})]
    if mode == "tool_delta":
        return [
            ToolCallDeltaEvent(
                **identity, item_index=0, call_id="c", name="delete", arguments_delta="{}"
            )
        ]
    if mode in {"oversize_delta", "unicode_delta"}:
        return [
            TextDeltaEvent(
                **identity, item_index=0, text="x" * 65537 if mode == "oversize_delta" else "\ud800"
            )
        ]
    if mode in {"malformed", "oversize_reply", "unicode_reply", "secret_reply"}:
        value = {
            "malformed": "not json",
            "oversize_reply": "x" * 65537,
            "unicode_reply": "\ud800",
            "secret_reply": "Bearer " + "synthetic_token_12345",
        }[mode]
        return [changed_text(event, value)]
    if mode == "no_assistant":
        return [
            event.model_copy(
                update={"turn": event.turn.model_copy(update={"assistant_messages": []})}
            )
        ]
    if mode == "non_text":
        return [
            event.model_copy(
                update={
                    "turn": event.turn.model_copy(
                        update={
                            "assistant_messages": [
                                AssistantMessage(
                                    item_index=0,
                                    content=[
                                        ImageReferencePart(
                                            artifact_id=UUID(int=1), media_type="image/png"
                                        )
                                    ],
                                )
                            ]
                        }
                    )
                }
            )
        ]
    return [event.model_copy(update={"stop_reason": StopReason(mode)})]


@pytest.mark.parametrize(
    "mode",
    [
        "failed",
        "exception",
        "missing_terminal",
        "extra_terminal",
        "attempt_id",
        "run_id",
        "sequence",
        "step_number",
        "internal_retry_count",
        "tool_delta",
        "oversize_delta",
        "unicode_delta",
        "malformed",
        "oversize_reply",
        "unicode_reply",
        "secret_reply",
        "no_assistant",
        "non_text",
        "max_tokens",
        "cancelled",
        "content_filter",
        "tool_use",
    ],
)
async def test_bad_provider_response_never_reaches_second_call(mode: str) -> None:
    tracked, _, job, group, executor = await setup()
    provider = ChangedProvider(tracked, lambda event: corrupt(event, mode))
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason in {"unavailable", "invalid_response"}, result
    assert result.review is None and result.prepared is None
    assert len(provider.requests) == len(result.calls) == 1
    assert "private provider diagnostic" not in result.model_dump_json()
    assert "synthetic_token" not in result.model_dump_json()
    async with tracked() as uow:
        assert (await uow.reconsolidation.renew(principal(), job.lease_token, NOW)).requests == 1
    assert result.calls[0].spend.state in {"settled", "unknown"}


@pytest.mark.parametrize(
    "usage_updates",
    [
        {"provider": "other"},
        {"model": "other"},
        {"input_tokens": 0},
        {"output_tokens": 0},
        {"cached_input_tokens": 101},
        {"cache_write_1h_input_tokens": 1},
        {"input_tokens": -1},
    ],
)
async def test_uncertifiable_usage_retains_full_reservation(usage_updates: dict[str, Any]) -> None:
    tracked, _, job, group, executor = await setup()

    def change(event: ModelCompletedEvent) -> list[ModelEvent]:
        return [
            event.model_copy(
                update={
                    "turn": event.turn.model_copy(
                        update={"usage": event.turn.usage.model_copy(update=usage_updates)}
                    )
                }
            )
        ]

    provider = ChangedProvider(tracked, change)
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason == "reviewed"
    assert all(
        c.spend.state == "unknown" and c.spend.charged_usd == c.spend.maximum_usd
        for c in result.calls
    )


async def test_provisional_usage_and_provider_cost_do_not_fake_a_free_call() -> None:
    tracked, _, job, group, executor = await setup()

    def change(event: ModelCompletedEvent) -> list[ModelEvent]:
        provisional = UsageEvent(
            attempt_id=event.attempt_id,
            run_id=event.run_id,
            step_number=event.step_number,
            sequence=0,
            usage=ModelUsage(),
        )
        return [provisional, event.model_copy(update={"sequence": 1})]

    provider = ChangedProvider(tracked, change)
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason == "reviewed"
    assert [c.spend.charged_usd for c in result.calls] == [Decimal("0.0002")] * 2


async def test_pinned_model_isolated_from_caller_and_adapter_mutation() -> None:
    tracked, _, job, group, executor = await setup()
    selected = model()

    class Mutating(ExecutionProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            assert resolved.model == "test-model" and resolved.pricing.output_per_mtok == 2
            async for event in super().stream(request, resolved, attempt):
                selected.model = "changed by caller"
                selected.pricing.output_per_mtok = Decimal("1000")
                resolved.model = "changed by adapter"
                resolved.pricing.output_per_mtok = Decimal("1000")
                request.metadata.clear()
                yield event

    result = await executor.run(
        job, (group.id,), model=selected, provider=Mutating(tracked), batch_id=UUID(int=90)
    )
    assert result.reason == "reviewed"
    assert [c.spend.charged_usd for c in result.calls] == [Decimal("0.0002")] * 2


@pytest.mark.parametrize("mode", ["disabled", "expired", "foreign", "wrong_provider", "in_uow"])
async def test_invalid_admission_makes_no_call(mode: str) -> None:
    tracked, clock, job, group, executor = await setup()
    provider = ExecutionProvider(tracked)
    selected = model()
    if mode == "disabled":
        executor._admitted = lambda: False
    if mode == "expired":
        clock.advance(timedelta(seconds=120))
    if mode == "foreign":
        job = job.model_copy(update={"lease_token": UUID(int=12345)})
    if mode == "wrong_provider":
        selected.provider = "other"

    async def run() -> None:
        result = await executor.run(
            job, (group.id,), model=selected, provider=provider, batch_id=UUID(int=90)
        )
        assert result.reason != "reviewed" and not result.calls and not provider.requests

    if mode == "in_uow":
        async with tracked():
            await run()
    else:
        await run()


@pytest.mark.parametrize(
    "mode",
    [
        "shutdown",
        "kill_switch",
        "lease_loss",
        "slice_deadline",
        "idle_timeout",
        "call_timeout",
        "wall_deadline",
    ],
)
async def test_inflight_cancellation_closes_stream_and_retains_spend(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked, clock, job, group, executor = await setup()
    entered, closed = asyncio.Event(), asyncio.Event()
    enabled = True
    executor._admitted = lambda: enabled
    monkeypatch.setattr("agent_core.memory.reconsolidation_execution.HEARTBEAT_SECONDS", 0.01)

    class Hanging(ExecutionProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            assert not tracked.is_open()
            self.requests.append(request)
            async with tracked() as uow:
                current = await uow.reconsolidation.renew(principal(), job.lease_token, clock.now())
                assert current.requests == 1 and current.slice_spent > 0
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            if False:
                yield cast(ModelEvent, None)

    provider = Hanging(tracked)
    limit = {
        "idle_timeout": "IDLE_SECONDS",
        "call_timeout": "CALL_SECONDS",
        "wall_deadline": "SLICE_SECONDS",
    }.get(mode)
    if limit:
        monkeypatch.setattr(f"agent_core.memory.reconsolidation_execution.{limit}", 0.03)
    task = asyncio.create_task(
        executor.run(job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90))
    )
    async with asyncio.timeout(2):
        await entered.wait()
        if mode == "shutdown":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if mode == "kill_switch":
                enabled = False
            elif mode == "slice_deadline":
                clock.advance(timedelta(seconds=120))
            elif mode == "lease_loss":
                async with tracked() as uow:
                    recovered = await uow.reconsolidation.claim_due(
                        principal(), NOW + timedelta(seconds=181), "recovery"
                    )
                    assert recovered is not None and recovered.lease_token != job.lease_token
            result = await task
            assert result.reason in {"admission_withdrawn", "lease_lost", "timeout"}, result
        assert closed.is_set() and len(provider.requests) == 1
    if mode != "lease_loss":
        async with tracked() as uow:
            current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
            assert current.requests == 1 and current.slice_spent > 0
            # A full unknown charge has settled even while the admission switch is off.
            await uow.reconsolidation.finish_group(
                principal(), job.lease_token, group.id, "retry", NOW
            )
            await uow.reconsolidation.release(principal(), job.lease_token, NOW)


@pytest.mark.parametrize("field,value", [("input_tokens", 100000), ("output_tokens", 4097)])
async def test_usage_over_admitted_token_ceiling_refuses_batch(field: str, value: int) -> None:
    tracked, _, job, group, executor = await setup()

    def exceed(event: ModelCompletedEvent) -> list[ModelEvent]:
        return [
            event.model_copy(
                update={
                    "turn": event.turn.model_copy(
                        update={"usage": event.turn.usage.model_copy(update={field: value})}
                    )
                }
            )
        ]

    provider = ChangedProvider(tracked, exceed)
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason == "invalid_response" and result.review is None
    assert len(provider.requests) == 1
    assert result.calls[0].spend.state == "unknown"
