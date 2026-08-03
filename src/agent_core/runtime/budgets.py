"""In-memory run budget ledger with pre-operation and record-time checks."""

from __future__ import annotations

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.messages import (
    ModelAttempt,
    ModelRequest,
    ModelUsage,
    ResolvedModel,
    StopReason,
)
from agent_core.domain.persistence import ModelCallRecord, ModelErrorKind, WorkerLease
from agent_core.domain.runs import BudgetScope, Run, RunUsage, Step
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.repositories import RunRepository

MILESTONE_2_MODEL_REGISTRY_VERSION = "fake-model-catalog@1"


def _restore_run_accounting(run: Run, snapshot: Run) -> None:
    run.usage = snapshot.usage.model_copy(deep=True)
    run.step_count = snapshot.step_count
    run.model_call_count = snapshot.model_call_count
    run.tool_call_count = snapshot.tool_call_count
    run.updated_at = snapshot.updated_at


def _check_before_operation(run: Run, scope: BudgetScope, clock: Clock) -> None:
    if run.deadline_at is not None and clock.now() >= run.deadline_at:
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
            raise BudgetExceededError("budget_exceeded", "the run input-token limit was reached")
        if (
            run.limits.max_output_tokens is not None
            and run.usage.output_tokens >= run.limits.max_output_tokens
        ):
            raise BudgetExceededError("budget_exceeded", "the run output-token limit was reached")


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


def _require_prefix_hash(request: ModelRequest) -> str:
    prefix_sha256 = request.metadata.get("prefix_sha256")
    if not isinstance(prefix_sha256, str) or len(prefix_sha256) != 64:
        raise RuntimeError("durable model usage requires a 64-character prefix_sha256")
    try:
        int(prefix_sha256, 16)
    except ValueError as exc:
        raise RuntimeError("durable model usage requires a hexadecimal prefix_sha256") from exc
    return prefix_sha256


class InMemoryBudgetLedger:
    def __init__(self, runs: RunRepository, clock: Clock) -> None:
        self._runs = runs
        self._clock = clock

    def check(self, run: Run, scope: BudgetScope) -> None:
        _check_before_operation(run, scope, self._clock)

    async def record_model_usage(
        self,
        run: Run,
        usage: ModelUsage,
        *,
        step: Step,
        attempt: ModelAttempt | None = None,
        request: ModelRequest | None = None,
        resolved_model: ResolvedModel | None = None,
        stop_reason: StopReason | None = None,
        error_kind: ModelErrorKind | None = None,
    ) -> None:
        del attempt, request, resolved_model, stop_reason, error_kind, step
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
        _check_after_record(run)

    async def record_tool_usage(self, run: Run, count: int, *, step: Step) -> None:
        del step
        run.tool_call_count += count
        run.usage = run.usage.model_copy(
            update={"tool_calls": run.usage.tool_calls + count}, deep=True
        )
        run.updated_at = self._clock.now()
        await self._runs.update_counters(run)
        _check_after_record(run)

    async def refund_orchestration_turn(self, run: Run, *, step: Step) -> None:
        del step
        if run.step_count > 0:
            run.step_count -= 1
        if run.model_call_count > 0:
            run.model_call_count -= 1
        run.updated_at = self._clock.now()
        await self._runs.update_counters(run)


class UnitOfWorkBudgetLedger:
    """Persist counters in short, fenced transactions for either storage tier."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        lease: WorkerLease | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._lease = lease

    def check(self, run: Run, scope: BudgetScope) -> None:
        _check_before_operation(run, scope, self._clock)

    async def record_model_usage(
        self,
        run: Run,
        usage: ModelUsage,
        *,
        step: Step,
        attempt: ModelAttempt | None = None,
        request: ModelRequest | None = None,
        resolved_model: ResolvedModel | None = None,
        stop_reason: StopReason | None = None,
        error_kind: ModelErrorKind | None = None,
    ) -> None:
        if attempt is None or request is None or resolved_model is None:
            raise RuntimeError("durable model usage requires attempt, request, and resolution")
        snapshot = run.model_copy(deep=True)
        reasoning = run.usage.reasoning_tokens
        if usage.reasoning_tokens is not None:
            reasoning = (reasoning or 0) + usage.reasoning_tokens
        run.usage = RunUsage(
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
        run.model_call_count += 1
        run.updated_at = self._clock.now()
        try:
            async with self._uow_factory() as uow:
                await uow.usage.record_attempt(
                    ModelCallRecord(
                        attempt_id=attempt.attempt_id,
                        run_id=run.id,
                        session_id=run.session_id,
                        tenant_id=run.tenant_id,
                        step_number=step.step_number,
                        attempt_number=attempt.attempt_number,
                        provider=resolved_model.provider,
                        model=resolved_model.model,
                        model_policy=resolved_model.policy_name,
                        registry_version=MILESTONE_2_MODEL_REGISTRY_VERSION,
                        prefix_sha256=_require_prefix_hash(request),
                        usage=usage,
                        cost=usage.cost,
                        cost_source=usage.cost_source,
                        stop_reason=stop_reason,
                        error_kind=error_kind,
                        started_at=attempt.started_at,
                        finished_at=self._clock.now(),
                    )
                )
                await uow.runs.update_counters(run, lease=self._lease)
        except BaseException:
            _restore_run_accounting(run, snapshot)
            raise
        _check_after_record(run)

    async def record_tool_usage(self, run: Run, count: int, *, step: Step) -> None:
        del step
        snapshot = run.model_copy(deep=True)
        run.tool_call_count += count
        run.usage = run.usage.model_copy(
            update={"tool_calls": run.usage.tool_calls + count}, deep=True
        )
        run.updated_at = self._clock.now()
        try:
            async with self._uow_factory() as uow:
                await uow.runs.update_counters(run, lease=self._lease)
        except BaseException:
            _restore_run_accounting(run, snapshot)
            raise
        _check_after_record(run)

    async def refund_orchestration_turn(self, run: Run, *, step: Step) -> None:
        del step
        snapshot = run.model_copy(deep=True)
        if run.step_count > 0:
            run.step_count -= 1
        if run.model_call_count > 0:
            run.model_call_count -= 1
        run.updated_at = self._clock.now()
        try:
            async with self._uow_factory() as uow:
                await uow.runs.update_counters(run, lease=self._lease)
        except BaseException:
            _restore_run_accounting(run, snapshot)
            raise
