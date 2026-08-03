from __future__ import annotations

from typing import Self, cast

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.errors import WorkerFencedError
from agent_core.domain.runs import Step
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from tests.contract.support import NOW, run


class _FencedRuns:
    async def update_counters(self, *_args: object, **_kwargs: object) -> None:
        raise WorkerFencedError("injected fence")


class _FailingUnitOfWork:
    runs = _FencedRuns()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FailingFactory:
    def __call__(self) -> _FailingUnitOfWork:
        return _FailingUnitOfWork()


async def test_failed_accounting_transaction_restores_the_run_snapshot() -> None:
    current = run()
    before = current.model_copy(deep=True)
    ledger = UnitOfWorkBudgetLedger(cast(UnitOfWorkFactory, _FailingFactory()), FixedClock(NOW))
    step = Step(run_id=current.id, step_number=1, started_at=NOW)

    with pytest.raises(WorkerFencedError):
        await ledger.record_tool_usage(current, 3, step=step)

    assert current == before
