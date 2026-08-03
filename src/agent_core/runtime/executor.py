"""Run execution and the only run-state transition writer."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import (
    BudgetExceededError,
    ConflictError,
    RunCancelledError,
    WorkerFencedError,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import AssistantMessage, ResolvedModel, TextPart, ToolCallItem
from agent_core.domain.persistence import ClaimedRun, WorkerLease
from agent_core.domain.runs import (
    CancelReason,
    FailureReason,
    OutcomeKind,
    Run,
    RunCheckpoint,
    RunFailure,
    RunOutcome,
    RunStatus,
    Step,
)
from agent_core.ports.context import ContextBuilder
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.models import ModelProvider
from agent_core.ports.persistence import CheckpointSeeder, UnitOfWorkFactory
from agent_core.ports.repositories import BudgetLedger, PrincipalResolver
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.loop import RunContext, ToolDispatch, checkpoint, run_loop

type BudgetFactory = Callable[[WorkerLease | None], BudgetLedger]
type TokenCallback = Callable[[RunCancellationToken], None]
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _FinalizationContext:
    run: Run
    checkpoint: RunCheckpoint
    uow_factory: UnitOfWorkFactory
    lease: WorkerLease | None
    clock: Clock
    token: RunCancellationToken


class RunExecutor:
    def __init__(
        self,
        *,
        principal: Principal,
        principals: PrincipalResolver,
        uow_factory: UnitOfWorkFactory,
        context_builder: ContextBuilder,
        model_provider: ModelProvider,
        resolved_model: ResolvedModel,
        budget_factory: BudgetFactory,
        clock: Clock,
        ids: IdFactory,
        dispatch_tools: ToolDispatch,
        seed_checkpoint: CheckpointSeeder,
        on_token: TokenCallback | None = None,
        max_internal_attempts: int = 3,
        identical_call_threshold: int = 5,
    ) -> None:
        self._principal = principal
        self._principals = principals
        self._uow_factory = uow_factory
        self._context_builder = context_builder
        self._model_provider = model_provider
        self._resolved_model = resolved_model
        self._budget_factory = budget_factory
        self._clock = clock
        self._ids = ids
        self._dispatch_tools = dispatch_tools
        self._seed_checkpoint = seed_checkpoint
        self._on_token = on_token
        self._max_internal_attempts = max_internal_attempts
        self._identical_call_threshold = identical_call_threshold

    async def execute(self, run_id: UUID) -> None:
        """Execute an in-process queued run after its creating commit."""

        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, self._principal)
            run = await uow.runs.transition(run.id, RunStatus.QUEUED, RunStatus.RUNNING)
            await uow.events.append(
                NewEvent(
                    session_id=run.session_id,
                    run_id=run.id,
                    event_type="run.started",
                    actor_type="runtime",
                    payload={"attempt": run.attempts + 1},
                )
            )
        await self._execute_running(run, lease=None)

    async def execute_claimed(
        self,
        claimed: ClaimedRun,
        *,
        on_token: TokenCallback | None = None,
    ) -> None:
        """Resume a run that a durable worker claimed and already marked running."""

        await self._execute_running(claimed.run, lease=claimed.lease, on_token=on_token)

    async def _execute_running(
        self,
        run: Run,
        *,
        lease: WorkerLease | None,
        on_token: TokenCallback | None = None,
    ) -> None:
        token = RunCancellationToken(self._clock, run.deadline_at)
        async with self._uow_factory() as uow:
            checkpoint_state = await uow.checkpoints.latest(run.id)
            if checkpoint_state is None:
                checkpoint_state = await self._seed_checkpoint(uow, run, None, lease)
        finalization = _FinalizationContext(
            run=run,
            checkpoint=checkpoint_state,
            uow_factory=self._uow_factory,
            lease=lease,
            clock=self._clock,
            token=token,
        )
        context: RunContext | None = None
        try:
            principal = await self._principals.for_run(run)
            async with self._uow_factory() as uow:
                agent = await uow.agents.get_version(run.agent_id, run.agent_version)
            callback = on_token or self._on_token
            if callback is not None:
                callback(token)
            context = RunContext(
                run=run,
                checkpoint=checkpoint_state,
                agent=agent,
                principal=principal,
                context_builder=self._context_builder,
                model_provider=self._model_provider,
                resolved_model=self._resolved_model,
                budgets=self._budget_factory(lease),
                uow_factory=self._uow_factory,
                lease=lease,
                clock=self._clock,
                ids=self._ids,
                token=token,
                dispatch_tools=self._dispatch_tools,
                max_internal_attempts=self._max_internal_attempts,
                identical_call_threshold=self._identical_call_threshold,
            )
            await self._resume_pending_tools(context)
            outcome = await run_loop(context)
        except RunCancelledError:
            outcome = RunOutcome(
                kind=(
                    OutcomeKind.FENCED
                    if token.reason is CancelReason.FENCED
                    else OutcomeKind.CANCELLED
                )
            )
        except BudgetExceededError as exc:
            reason = (
                FailureReason.MAX_STEPS_EXCEEDED
                if exc.reason == FailureReason.MAX_STEPS_EXCEEDED.value
                else FailureReason.DEADLINE_EXCEEDED
                if exc.reason == FailureReason.DEADLINE_EXCEEDED.value
                else FailureReason.BUDGET_EXCEEDED
            )
            outcome = RunOutcome(
                kind=OutcomeKind.FAILED,
                failure=RunFailure(
                    reason=reason,
                    error_class=type(exc).__name__,
                    message=str(exc),
                    step_number=run.step_count or None,
                    occurred_at=self._clock.now(),
                ),
            )
        except WorkerFencedError:
            outcome = RunOutcome(kind=OutcomeKind.FENCED)
        except ConflictError as exc:
            logger.exception(
                "run_execution_failed",
                extra={"run_id": str(run.id), "error_class": type(exc).__name__},
            )
            outcome = self._internal_failure(run, exc)
        except Exception as exc:
            logger.exception(
                "run_execution_failed",
                extra={"run_id": str(run.id), "error_class": type(exc).__name__},
            )
            outcome = self._internal_failure(run, exc)
        try:
            await finalize(context or finalization, outcome)
        except WorkerFencedError:
            if lease is None:
                raise

    async def _resume_pending_tools(self, context: RunContext) -> None:
        if not context.checkpoint.pending_tool_calls:
            return
        calls = [
            ToolCallItem.model_validate(call) for call in context.checkpoint.pending_tool_calls
        ]
        step = Step(
            run_id=context.run.id,
            step_number=max(1, context.run.step_count),
            started_at=self._clock.now(),
        )
        results = await context.dispatch_tools(
            run=context.run,
            checkpoint=context.checkpoint,
            tool_calls=calls,
            principal=context.principal,
            step=step,
            agent=context.agent,
            token=context.token,
            lease=context.lease,
        )
        step.tool_call_count = len(results)
        baseline = context.checkpoint.budget_state.get("tool_call_count", 0)
        if not isinstance(baseline, int):
            raise ConflictError("checkpoint tool-usage watermark is malformed")
        recorded = context.run.tool_call_count - baseline
        if recorded == 0:
            await context.budgets.record_tool_usage(context.run, len(results), step=step)
        elif recorded != len(results):
            raise ConflictError("persisted tool usage does not match the pending batch")
        markers = context.checkpoint.working_state.setdefault("tool_usage_recorded_steps", {})
        if not isinstance(markers, dict):
            raise ConflictError("checkpoint tool-usage marker is malformed")
        markers[str(step.step_number)] = len(results)
        context.checkpoint.conversation.extend(results)
        context.checkpoint.pending_tool_calls = []
        await checkpoint(context, "tool_recovered")

    def _internal_failure(self, run: Run, exc: Exception) -> RunOutcome:
        return RunOutcome(
            kind=OutcomeKind.FAILED,
            failure=RunFailure(
                reason=FailureReason.INTERNAL_ERROR,
                error_class=type(exc).__name__,
                message="an unexpected internal error ended the run",
                step_number=run.step_count or None,
                occurred_at=self._clock.now(),
            ),
        )


def _message_text(message: AssistantMessage | None) -> str | None:
    if message is None:
        return None
    return "\n".join(part.text for part in message.content if isinstance(part, TextPart))


async def finalize(context: RunContext | _FinalizationContext, outcome: RunOutcome) -> None:
    """Commit one terminal transaction, falling back to FAILED on local errors."""

    original_checkpoint = context.checkpoint.model_copy(deep=True)
    try:
        await _finalize_once(context, outcome)
    except WorkerFencedError:
        raise
    except Exception as exc:
        context.checkpoint = original_checkpoint
        logger.exception(
            "run_finalization_failed",
            extra={"run_id": str(context.run.id), "error_class": type(exc).__name__},
        )
        failure = RunFailure(
            reason=FailureReason.INTERNAL_ERROR,
            error_class=type(exc).__name__,
            message="an unexpected finalization error ended the run",
            step_number=context.run.step_count or None,
            occurred_at=context.clock.now(),
        )
        await _finalize_once(
            context,
            RunOutcome(kind=OutcomeKind.FAILED, failure=failure),
        )


async def _finalize_once(context: RunContext | _FinalizationContext, outcome: RunOutcome) -> None:
    """Apply one terminal outcome without retrying its persistence operations."""

    if outcome.kind is OutcomeKind.FENCED:
        return
    if outcome.kind is OutcomeKind.COMPLETED:
        message = AssistantMessage.model_validate(outcome.final_message)
        status = RunStatus.COMPLETED
        event_type = "run.completed"
        payload: dict[str, Any] = {"final_message": message.model_dump(mode="json")}
        failure = None
        final_message = _message_text(message)
    elif outcome.kind is OutcomeKind.CANCELLED:
        status = RunStatus.CANCELLED
        event_type = "run.cancelled"
        payload = {"reason": getattr(context.token.reason, "value", "requested")}
        failure = None
        final_message = None
    elif outcome.kind is OutcomeKind.FAILED:
        if outcome.failure is None:
            raise RuntimeError("failed outcome requires a RunFailure")
        status = RunStatus.FAILED
        event_type = "run.failed"
        payload = {"failure": outcome.failure.model_dump(mode="json")}
        failure = outcome.failure
        final_message = None
    else:
        raise RuntimeError(f"Milestone 2 cannot finalize outcome {outcome.kind.value}")

    context.checkpoint.version += 1
    context.checkpoint.status = status
    context.checkpoint.created_at = context.clock.now()
    async with context.uow_factory() as uow:
        terminal_event = await uow.events.append(
            NewEvent(
                session_id=context.run.session_id,
                run_id=context.run.id,
                event_type=event_type,
                actor_type="runtime",
                payload=payload,
            ),
            lease=context.lease,
        )
        context.checkpoint.last_event_sequence = terminal_event.sequence
        await uow.checkpoints.write(
            context.run.id,
            context.checkpoint,
            full=True,
            lease=context.lease,
        )
        await uow.runs.transition(
            context.run.id,
            RunStatus.RUNNING,
            status,
            failure=failure,
            final_message=final_message,
            lease=context.lease,
        )
        if context.lease is not None:
            if uow.queue is None:
                raise RuntimeError("durable lease has no queue repository")
            await uow.queue.release(context.lease, status)
