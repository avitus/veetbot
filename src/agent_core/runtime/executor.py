"""Run execution and the only run-state transition writer."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    ConflictError,
    RunCancelledError,
    UserInputRequiredError,
    WorkerFencedError,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    AssistantMessage,
    ResolvedModel,
    TextPart,
    ToolCallItem,
    ToolResultItem,
)
from agent_core.domain.persistence import ClaimedRun, WorkerLease
from agent_core.domain.policies import TrustLevel
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
from agent_core.domain.tools import ToolInvocationStatus, ToolOutcome, ToolOutcomeStatus
from agent_core.model import NON_ROUTED_MODEL_POLICIES
from agent_core.ports.context import ContextBuilder
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.models import ModelProvider, ModelRouter
from agent_core.ports.persistence import (
    CheckpointSeeder,
    RepositoryUnitOfWork,
    UnitOfWorkFactory,
)
from agent_core.ports.repositories import BudgetLedger, PrincipalResolver
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.loop import (
    ModelEventCallback,
    RunContext,
    ToolDispatch,
    checkpoint,
    run_loop,
)

type BudgetFactory = Callable[[WorkerLease | None], BudgetLedger]
type TokenCallback = Callable[[UUID, RunCancellationToken], None]
type TokenCompleteCallback = Callable[[UUID], None]
type RunCompleteCallback = Callable[[UUID, int | None], Awaitable[None]]
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
        model_router: ModelRouter | None = None,
        model_providers: Mapping[str, ModelProvider] | None = None,
        budget_factory: BudgetFactory,
        clock: Clock,
        ids: IdFactory,
        dispatch_tools: ToolDispatch,
        seed_checkpoint: CheckpointSeeder,
        on_token: TokenCallback | None = None,
        on_token_complete: TokenCompleteCallback | None = None,
        on_run_complete: RunCompleteCallback | None = None,
        on_model_event: ModelEventCallback | None = None,
        max_internal_attempts: int = 3,
        identical_call_threshold: int = 5,
        identical_denial_threshold: int = 3,
    ) -> None:
        self._principal = principal
        self._principals = principals
        self._uow_factory = uow_factory
        self._context_builder = context_builder
        self._model_provider = model_provider
        self._resolved_model = resolved_model
        self._model_router = model_router
        self._model_providers = dict(model_providers or {})
        self._budget_factory = budget_factory
        self._clock = clock
        self._ids = ids
        self._dispatch_tools = dispatch_tools
        self._seed_checkpoint = seed_checkpoint
        self._on_token = on_token
        self._on_token_complete = on_token_complete
        self._on_run_complete = on_run_complete
        self._on_model_event = on_model_event
        self._max_internal_attempts = max_internal_attempts
        self._identical_call_threshold = identical_call_threshold
        self._identical_denial_threshold = identical_denial_threshold

    async def requeue_after_approval(self, uow: RepositoryUnitOfWork, run: Run) -> Run:
        """Apply the guarded approval-resume edge in the sole run-state writer."""

        return await uow.runs.transition(
            run.id,
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.QUEUED,
        )

    async def requeue_after_input(self, uow: RepositoryUnitOfWork, run: Run) -> Run:
        """Apply the guarded user-input resume edge in the sole state writer."""

        return await uow.runs.transition(
            run.id,
            RunStatus.WAITING_FOR_USER,
            RunStatus.QUEUED,
        )

    async def cancel_parked_run(
        self,
        uow: RepositoryUnitOfWork,
        run: Run,
        principal_id: str,
    ) -> Run:
        """Cancel a queued or approval-waiting run and reap its approval."""

        if run.status is RunStatus.WAITING_FOR_APPROVAL:
            await uow.approvals.cancel_for_run(run.id)
        if run.status is RunStatus.WAITING_FOR_USER:
            owner = Principal(
                tenant_id=run.tenant_id,
                principal_id=principal_id,
            )
            invocations = await uow.invocations.list_for_run(run.id, owner)
            for invocation in invocations:
                if (
                    invocation.status is not ToolInvocationStatus.RUNNING
                    or invocation.suspended_kind != "user_input"
                ):
                    continue
                outcome = ToolOutcome(
                    status=ToolOutcomeStatus.FAILED,
                    action=invocation.tool_name,
                    reason_code="tool.run_cancelled",
                    message="The run was cancelled while waiting for user input.",
                    retryable=False,
                    remediation="none",
                )
                result_item = ToolResultItem(
                    call_id=invocation.call_id,
                    content=[TextPart(text=outcome.message)],
                    is_error=True,
                    trust=TrustLevel.PLATFORM,
                )
                failed = invocation.model_copy(
                    update={
                        "status": ToolInvocationStatus.FAILED,
                        "suspended_kind": None,
                        "suspended_ref": None,
                        "outcome": outcome,
                        "result_item": result_item,
                        "updated_at": self._clock.now(),
                    },
                    deep=True,
                )
                await uow.invocations.transition(
                    invocation.id,
                    ToolInvocationStatus.RUNNING,
                    failed,
                )
        cancelled = await uow.runs.transition(run.id, run.status, RunStatus.CANCELLED)
        await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="run.cancelled",
                actor_type="principal",
                actor_id=principal_id,
                payload={"reason": "requested"},
            )
        )
        return cancelled

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
        try:
            await self._execute_running(run, lease=None)
        finally:
            await self._complete_run(run.id, None)

    async def execute_claimed(
        self,
        claimed: ClaimedRun,
        *,
        on_token: TokenCallback | None = None,
    ) -> None:
        """Resume a run that a durable worker claimed and already marked running."""

        try:
            await self._execute_running(claimed.run, lease=claimed.lease, on_token=on_token)
        finally:
            await self._complete_run(claimed.run.id, claimed.lease.lease_epoch)

    async def _complete_run(self, run_id: UUID, lease_epoch: int | None) -> None:
        try:
            if self._on_run_complete is not None:
                await self._on_run_complete(run_id, lease_epoch)
        except Exception:
            logger.exception("run_resource_cleanup_failed", extra={"run_id": str(run_id)})
        finally:
            self._complete_token(run_id)

    def _complete_token(self, run_id: UUID) -> None:
        if self._on_token_complete is not None:
            self._on_token_complete(run_id)

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
            model_provider = self._model_provider
            resolved_model = self._resolved_model
            pin_created = False
            if (
                self._model_router is not None
                and agent.model_policy not in NON_ROUTED_MODEL_POLICIES
            ):
                if checkpoint_state.provider_pin is None and run.provider_pin is not None:
                    checkpoint_state.provider_pin = run.provider_pin.model_copy(deep=True)
                if checkpoint_state.provider_pin is None:
                    resolved_model = await self._model_router.resolve(
                        agent.model_policy,
                        tenant_id=run.tenant_id,
                    )
                    checkpoint_state.provider_pin = self._model_router.pin(run.id, resolved_model)
                    run.provider_pin = checkpoint_state.provider_pin.model_copy(deep=True)
                    async with self._uow_factory() as uow:
                        await uow.runs.set_provider_pin(run.id, run.provider_pin)
                    pin_created = True
                else:
                    resolved_model = await self._model_router.resolve_pinned(
                        checkpoint_state.provider_pin
                    )
                    resolved_model = resolved_model.model_copy(
                        update={"policy_name": agent.model_policy}
                    )
                if (
                    checkpoint_state.provider_continuation is not None
                    and checkpoint_state.provider_continuation.provider != resolved_model.provider
                ):
                    raise ConflictError("provider continuation does not match the persisted pin")
                selected = self._model_providers.get(resolved_model.provider)
                if selected is None:
                    raise RuntimeError("the pinned model provider has no registered adapter")
                model_provider = selected
            callback = on_token or self._on_token
            if callback is not None:
                callback(run.id, token)
            context = RunContext(
                run=run,
                checkpoint=checkpoint_state,
                agent=agent,
                principal=principal,
                context_builder=self._context_builder,
                model_provider=model_provider,
                resolved_model=resolved_model,
                budgets=self._budget_factory(lease),
                uow_factory=self._uow_factory,
                lease=lease,
                clock=self._clock,
                ids=self._ids,
                token=token,
                dispatch_tools=self._dispatch_tools,
                on_model_event=self._on_model_event,
                max_internal_attempts=self._max_internal_attempts,
                identical_call_threshold=self._identical_call_threshold,
                identical_denial_threshold=self._identical_denial_threshold,
            )
            if pin_created:
                await checkpoint(context, "provider_pinned")
            try:
                await self._resume_pending_tools(context)
            except ApprovalRequiredError as exc:
                if exc.approval_id not in context.checkpoint.pending_approval_ids:
                    context.checkpoint.pending_approval_ids.append(exc.approval_id)
                await checkpoint(context, "suspended")
                outcome = RunOutcome(
                    kind=OutcomeKind.SUSPENDED,
                    suspension={
                        "kind": "approval",
                        "approval_id": str(exc.approval_id),
                    },
                )
            except UserInputRequiredError as exc:
                context.checkpoint.working_state["outstanding_question_id"] = str(exc.question_id)
                await checkpoint(context, "suspended")
                outcome = RunOutcome(
                    kind=OutcomeKind.SUSPENDED,
                    suspension={
                        "kind": "user",
                        "question_id": str(exc.question_id),
                        "invocation_id": str(exc.invocation_id),
                    },
                )
            else:
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
        # The queued generation is revalidating the persisted calls. Any approval
        # raised below becomes the sole pending suspension for the next generation.
        context.checkpoint.pending_approval_ids = []
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
        if outcome.kind is OutcomeKind.SUSPENDED:
            async with context.uow_factory() as uow:
                await uow.approvals.cancel_for_run(context.run.id)
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
    elif outcome.kind is OutcomeKind.SUSPENDED:
        suspension = outcome.suspension if isinstance(outcome.suspension, dict) else {}
        if suspension.get("kind") == "user":
            status = RunStatus.WAITING_FOR_USER
            event_type = "run.waiting_for_user"
        else:
            status = RunStatus.WAITING_FOR_APPROVAL
            event_type = "run.waiting_for_approval"
        payload = {"suspension": outcome.suspension}
        if status is RunStatus.WAITING_FOR_USER:
            payload["question_id"] = suspension.get("question_id")
        failure = None
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
        last_event = terminal_event
        if outcome.kind is OutcomeKind.SUSPENDED and status is RunStatus.WAITING_FOR_APPROVAL:
            approval_id = (
                context.checkpoint.pending_approval_ids[0]
                if context.checkpoint.pending_approval_ids
                else None
            )
            if approval_id is None:
                raise RuntimeError("approval suspension has no pending approval id")
            last_event = await uow.events.append(
                NewEvent(
                    session_id=context.run.session_id,
                    run_id=context.run.id,
                    event_type="approval.requested",
                    actor_type="runtime",
                    payload={"approval_id": str(approval_id)},
                ),
                lease=context.lease,
            )
        context.checkpoint.last_event_sequence = last_event.sequence
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
