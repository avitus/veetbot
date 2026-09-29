from __future__ import annotations

from decimal import Decimal
from typing import Self, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.errors import BudgetExceededError, WorkerFencedError
from agent_core.domain.messages import (
    ModelAttempt,
    ModelRequest,
    ModelUsage,
    ResolvedModel,
    StopReason,
    TextPart,
    UserMessage,
)
from agent_core.domain.runs import RunLimits, Step
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from tests.contract.support import NOW, RUN_ID, memory_uow_factory, principal, run

PREFIX = "a" * 64


class _FencedRuns:
    async def update_counters(self, *_args: object, **_kwargs: object) -> None:
        raise WorkerFencedError("injected fence")


class _FencedUsage:
    def __init__(self) -> None:
        self.recorded: list[object] = []

    async def record_attempt(self, call: object) -> None:
        self.recorded.append(call)


class _FailingUnitOfWork:
    runs = _FencedRuns()

    def __init__(self) -> None:
        self.usage = _FencedUsage()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FailingFactory:
    def __call__(self) -> _FailingUnitOfWork:
        return _FailingUnitOfWork()


def _attempt() -> ModelAttempt:
    return ModelAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-00000000b001"),
        run_id=RUN_ID,
        step_number=1,
        attempt_number=1,
        started_at=NOW,
    )


def _request(prefix: str | None = PREFIX) -> ModelRequest:
    return ModelRequest(
        model_policy="balanced",
        conversation=[UserMessage(content=[TextPart(text="hello")])],
        tools=[],
        metadata={} if prefix is None else {"prefix_sha256": prefix},
    )


def _resolved() -> ResolvedModel:
    return ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)


def _step() -> Step:
    return Step(run_id=RUN_ID, step_number=1, started_at=NOW)


async def test_failed_accounting_transaction_restores_the_run_snapshot() -> None:
    current = run()
    before = current.model_copy(deep=True)
    ledger = UnitOfWorkBudgetLedger(cast(UnitOfWorkFactory, _FailingFactory()), FixedClock(NOW))
    step = Step(run_id=current.id, step_number=1, started_at=NOW)

    with pytest.raises(WorkerFencedError):
        await ledger.record_tool_usage(current, 3, step=step)

    assert current == before


async def test_a_fenced_model_usage_write_or_refund_restores_the_run_snapshot() -> None:
    current = run().model_copy(update={"step_count": 2, "model_call_count": 2})
    before = current.model_copy(deep=True)
    ledger = UnitOfWorkBudgetLedger(cast(UnitOfWorkFactory, _FailingFactory()), FixedClock(NOW))

    with pytest.raises(WorkerFencedError):
        await ledger.record_model_usage(
            current,
            ModelUsage(input_tokens=5, output_tokens=2, cost=Decimal("0.01")),
            step=_step(),
            attempt=_attempt(),
            request=_request(),
            resolved_model=_resolved(),
        )
    assert current == before

    with pytest.raises(WorkerFencedError):
        await ledger.refund_orchestration_turn(current, step=_step())
    assert current == before


@pytest.mark.parametrize(
    "prefix",
    [None, "a" * 63, "g" * 64],
    ids=["absent", "short", "not_hexadecimal"],
)
async def test_durable_usage_requires_a_hexadecimal_prefix_hash_and_writes_nothing_without_one(
    prefix: str | None,
) -> None:
    _clock, factory = await memory_uow_factory()
    current = run()
    async with factory() as uow:
        await uow.runs.create(current)
    before = current.model_copy(deep=True)
    ledger = UnitOfWorkBudgetLedger(factory, FixedClock(NOW))

    with pytest.raises(RuntimeError, match="prefix_sha256"):
        await ledger.record_model_usage(
            current,
            ModelUsage(input_tokens=5),
            step=_step(),
            attempt=_attempt(),
            request=_request(prefix),
            resolved_model=_resolved(),
        )

    assert current == before
    async with factory() as uow:
        assert (await uow.usage.run_usage(RUN_ID)).model_calls == 0
        assert (await uow.runs.get(RUN_ID, principal())).model_call_count == 0


@pytest.mark.parametrize("missing", ["attempt", "request", "resolved_model"])
async def test_durable_usage_refuses_an_attempt_it_cannot_attribute(missing: str) -> None:
    current = run()
    before = current.model_copy(deep=True)
    ledger = UnitOfWorkBudgetLedger(cast(UnitOfWorkFactory, _FailingFactory()), FixedClock(NOW))
    attribution: dict[str, object] = {
        "attempt": _attempt(),
        "request": _request(),
        "resolved_model": _resolved(),
    }
    attribution[missing] = None

    with pytest.raises(RuntimeError, match="requires attempt, request, and resolution"):
        await ledger.record_model_usage(
            current,
            ModelUsage(input_tokens=5),
            step=_step(),
            **attribution,  # type: ignore[arg-type]
        )

    assert current == before


async def test_durable_usage_writes_the_call_row_and_counters_then_raises_when_over() -> None:
    """The model_calls row and the run totals land together, even for the over-budget call."""

    _clock, factory = await memory_uow_factory()
    current = run().model_copy(
        update={
            "limits": RunLimits(
                max_steps=4, max_model_calls=4, max_tool_calls=4, max_output_tokens=5
            )
        }
    )
    async with factory() as uow:
        await uow.runs.create(current)
    ledger = UnitOfWorkBudgetLedger(factory, FixedClock(NOW))

    with pytest.raises(BudgetExceededError, match="output-token"):
        await ledger.record_model_usage(
            current,
            ModelUsage(input_tokens=7, output_tokens=6, cost=Decimal("0.02")),
            step=_step(),
            attempt=_attempt(),
            request=_request(),
            resolved_model=_resolved(),
            stop_reason=StopReason.END_TURN,
        )

    async with factory() as uow:
        recorded = await uow.usage.run_usage(RUN_ID)
        stored = await uow.runs.get(RUN_ID, principal())
    assert (recorded.model_calls, recorded.input_tokens, recorded.output_tokens) == (1, 7, 6)
    assert recorded.cost == Decimal("0.02")
    assert stored.model_call_count == current.model_call_count == 1
    assert stored.usage.output_tokens == current.usage.output_tokens == 6
