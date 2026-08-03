"""In-memory run budget ledger with pre-operation and record-time checks."""

from __future__ import annotations

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.messages import ModelUsage
from agent_core.domain.runs import BudgetScope, Run, RunUsage, Step
from agent_core.ports.determinism import Clock
from agent_core.ports.repositories import RunRepository


class InMemoryBudgetLedger:
    def __init__(self, runs: RunRepository, clock: Clock) -> None:
        self._runs = runs
        self._clock = clock

    def check(self, run: Run, scope: BudgetScope) -> None:
        if run.deadline_at is not None and self._clock.now() >= run.deadline_at:
            raise BudgetExceededError("deadline_exceeded", "the run deadline elapsed")
        if run.limits.max_cost is not None and run.usage.cost >= run.limits.max_cost:
            raise BudgetExceededError("budget_exceeded", "the run cost limit was reached")
        if scope is BudgetScope.STEP:
            if run.step_count >= run.limits.max_steps:
                raise BudgetExceededError("max_steps_exceeded", "the run step limit was reached")
            if run.tool_call_count >= run.limits.max_tool_calls:
                raise BudgetExceededError("budget_exceeded", "the run tool-call limit was reached")
        if scope is BudgetScope.ATTEMPT:
            if run.model_call_count >= run.limits.max_model_calls:
                raise BudgetExceededError("budget_exceeded", "the run model-call limit was reached")
            if (
                run.limits.max_input_tokens is not None
                and run.usage.input_tokens >= run.limits.max_input_tokens
            ):
                raise BudgetExceededError(
                    "budget_exceeded", "the run input-token limit was reached"
                )
            if (
                run.limits.max_output_tokens is not None
                and run.usage.output_tokens >= run.limits.max_output_tokens
            ):
                raise BudgetExceededError(
                    "budget_exceeded", "the run output-token limit was reached"
                )

    async def record_model_usage(self, run: Run, usage: ModelUsage, *, step: Step) -> None:
        del step
        reasoning = run.usage.reasoning_tokens
        if usage.reasoning_tokens is not None:
            reasoning = (reasoning or 0) + usage.reasoning_tokens
        updated = RunUsage(
            input_tokens=run.usage.input_tokens + usage.input_tokens,
            cached_input_tokens=run.usage.cached_input_tokens + usage.cached_input_tokens,
            cache_write_input_tokens=(
                run.usage.cache_write_input_tokens + usage.cache_write_input_tokens
            ),
            output_tokens=run.usage.output_tokens + usage.output_tokens,
            reasoning_tokens=reasoning,
            model_calls=run.usage.model_calls + 1,
            tool_calls=run.usage.tool_calls,
            cost=run.usage.cost + usage.cost,
        )
        run.usage = updated
        run.model_call_count += 1
        run.updated_at = self._clock.now()
        await self._runs.update_counters(run)
        self._check_after_record(run)

    async def record_tool_usage(self, run: Run, count: int, *, step: Step) -> None:
        del step
        run.tool_call_count += count
        run.usage = run.usage.model_copy(
            update={"tool_calls": run.usage.tool_calls + count}, deep=True
        )
        run.updated_at = self._clock.now()
        await self._runs.update_counters(run)
        self._check_after_record(run)

    async def refund_orchestration_turn(self, run: Run, *, step: Step) -> None:
        del step
        if run.step_count > 0:
            run.step_count -= 1
        if run.model_call_count > 0:
            run.model_call_count -= 1
        run.updated_at = self._clock.now()
        await self._runs.update_counters(run)

    @staticmethod
    def _check_after_record(run: Run) -> None:
        if run.model_call_count > run.limits.max_model_calls:
            raise BudgetExceededError("budget_exceeded", "model-call budget exceeded")
        if run.tool_call_count > run.limits.max_tool_calls:
            raise BudgetExceededError("budget_exceeded", "tool-call budget exceeded")
        if run.limits.max_input_tokens is not None and (
            run.usage.input_tokens > run.limits.max_input_tokens
        ):
            raise BudgetExceededError("budget_exceeded", "input-token budget exceeded")
        if run.limits.max_output_tokens is not None and (
            run.usage.output_tokens > run.limits.max_output_tokens
        ):
            raise BudgetExceededError("budget_exceeded", "output-token budget exceeded")
        if run.limits.max_cost is not None and run.usage.cost > run.limits.max_cost:
            raise BudgetExceededError("budget_exceeded", "cost budget exceeded")
