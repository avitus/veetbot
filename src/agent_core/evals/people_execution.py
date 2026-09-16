"""Cost accounting for isolated People comparisons; no activation claims."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.messages import (
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ResolvedModel,
)
from agent_core.ports.models import ModelProvider
from agent_core.runtime.people_imports import reservation_cost


class EvaluationBudget:
    """One finite allowance shared across all cases, arms, and repeats.

    A reservation is journaled before provider access. An interrupted call retains
    that reservation; a restart cannot overwrite the prior run and hide its costs.
    """

    def __init__(self, maximum: Decimal, journal: Path) -> None:
        if not maximum.is_finite() or maximum <= 0:
            raise ValueError("evaluation requires a finite positive monetary cap")
        self.maximum = maximum
        self.journal = journal
        with journal.open("x", encoding="utf-8"):
            pass
        self.spent = Decimal(0)
        self.held = Decimal(0)
        self.calls = 0
        self.failed = False
        self.lock = asyncio.Lock()

    def append(self, record: dict[str, object]) -> None:
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class BudgetedProvider:
    def __init__(self, provider: ModelProvider, budget: EvaluationBudget) -> None:
        self.provider = provider
        self.name = provider.name
        self.budget = budget

    async def close(self) -> None:
        await self.provider.close()

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        reserve = reservation_cost(request, resolved)
        ledger = self.budget
        async with ledger.lock:
            if ledger.failed or ledger.spent + ledger.held + reserve > ledger.maximum:
                raise BudgetExceededError(
                    "cost", "People evaluation exhausted its approved allowance"
                )
            index = ledger.calls + 1
            record: dict[str, object] = {
                "index": index,
                "provider": resolved.provider,
                "model": resolved.model,
                "request_sha256": hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
                "reservation_usd": str(reserve),
                "outcome": "reserved",
            }
            ledger.append(record)
            ledger.calls = index
            ledger.held += reserve
        completed = False
        try:
            async for event in self.provider.stream(request, resolved, attempt):
                if isinstance(event, ModelCompletedEvent):
                    actual = event.turn.usage.cost
                    if completed or not actual.is_finite() or actual < 0:
                        raise ValueError("evaluation received invalid final usage")
                    async with ledger.lock:
                        ledger.append(
                            {
                                **record,
                                "outcome": "overrun" if actual > reserve else "completed",
                                "actual_cost_usd": str(actual),
                            }
                        )
                        ledger.held -= reserve
                        ledger.spent += actual
                        completed = True
                        if actual > reserve:
                            ledger.failed = True
                            raise BudgetExceededError(
                                "cost", "People evaluation usage exceeded its reservation"
                            )
                yield event
            if not completed:
                raise ValueError("evaluation provider ended without final usage")
        except BaseException:
            ledger.failed = True
            raise
        finally:
            if not completed:
                async with ledger.lock:
                    ledger.failed = True
                    ledger.append({**record, "outcome": "uncertain"})


class BorrowedProvider:
    """A case composition borrows the run-owned provider and cannot close it."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self.name = provider.name

    async def close(self) -> None:
        return None

    def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        return self.provider.stream(request, resolved, attempt)
