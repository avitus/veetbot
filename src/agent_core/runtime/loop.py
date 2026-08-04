"""The provider-neutral run loop; it computes an outcome and ends no run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Protocol, cast

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.context import ContextPlan, WorkingState
from agent_core.domain.errors import ApprovalRequiredError, ContextOverflow, UserInputRequiredError
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    AssistantMessage,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelRequest,
    ModelTransientError,
    ModelTurn,
    ModelUsage,
    ResolvedModel,
    StopReason,
    TextPart,
    ToolCallItem,
    ToolResultItem,
)
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import (
    BudgetScope,
    FailureReason,
    OutcomeKind,
    ProviderContinuation,
    Run,
    RunCheckpoint,
    RunFailure,
    RunOutcome,
    Step,
)
from agent_core.domain.tools import ToolOutcome, ToolOutcomeStatus
from agent_core.model.streaming import ModelStreamError, validated_stream
from agent_core.ports.context import Compactor, PressureAwareContextBuilder, TokenEstimator
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import CancellationToken
from agent_core.ports.models import ModelProvider
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.repositories import BudgetLedger

type ToolDispatch = Callable[..., Awaitable[list[ToolResultItem]]]
type ModelEventCallback = Callable[[Run, ModelEvent], Awaitable[None]]
type AddOpenQuestion = Callable[[WorkingState, str], WorkingState]


@dataclass(slots=True)
class RunContext:
    run: Run
    checkpoint: RunCheckpoint
    agent: AgentSpec
    principal: Principal
    context_builder: PressureAwareContextBuilder
    context_plan: ContextPlan
    compactor: Compactor
    token_estimator: TokenEstimator
    model_provider: ModelProvider
    resolved_model: ResolvedModel
    budgets: BudgetLedger
    uow_factory: UnitOfWorkFactory
    lease: WorkerLease | None
    clock: Clock
    ids: IdFactory
    token: CancellationToken
    dispatch_tools: ToolDispatch
    add_open_question: AddOpenQuestion
    on_model_event: ModelEventCallback | None = None
    max_internal_attempts: int = 3
    identical_call_threshold: int = 5
    identical_denial_threshold: int = 3
    max_compactions_per_step: int = 2


class CheckpointContext(Protocol):
    run: Run
    checkpoint: RunCheckpoint
    uow_factory: UnitOfWorkFactory
    lease: WorkerLease | None
    clock: Clock


async def _append_event(
    context: CheckpointContext, event_type: str, payload: dict[str, Any] | None = None
) -> None:
    async with context.uow_factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=context.run.session_id,
                run_id=context.run.id,
                event_type=event_type,
                actor_type="runtime",
                payload=payload or {},
            ),
            lease=context.lease,
        )


def _record_open_question(
    checkpoint: RunCheckpoint,
    question: str,
    add_open_question: AddOpenQuestion,
) -> WorkingState:
    raw = checkpoint.working_state.get("context")
    state = WorkingState() if raw is None else WorkingState.model_validate(raw)
    updated = add_open_question(state, question)
    checkpoint.working_state["context"] = updated.model_dump(mode="json")
    checkpoint.working_state["outstanding_question_text"] = question
    return updated


async def build_with_pressure(context: RunContext, step: Step) -> ModelRequest:
    """Measure, compact through a checkpoint write, and only then build."""

    while True:
        context.checkpoint.budget_state["context_model_id"] = (
            f"{context.resolved_model.provider}:{context.resolved_model.model}"
        )
        context.checkpoint.budget_state["context_seed_event_sequence"] = (
            context.run.seed_event_sequence
        )
        assembled = await context.context_builder.assemble(
            context.run,
            context.checkpoint,
            context.agent,
            context.principal,
        )
        pressure = assembled.pressure
        if pressure.fits:
            return assembled.request
        await _append_event(
            context,
            "context.budget.pressure",
            {
                "step_number": step.step_number,
                "reason": pressure.reason,
                "total_tokens": pressure.total_tokens,
                "capacity_tokens": pressure.capacity_tokens,
                "yield_steps": list(pressure.yield_steps),
            },
        )
        if not pressure.compactable or step.compactions >= context.max_compactions_per_step:
            await _append_event(
                context,
                "context.budget.exceeded",
                {
                    "step_number": step.step_number,
                    "reason": pressure.reason,
                    "compactions": step.compactions,
                },
            )
            raise ContextOverflow(pressure.reason)
        updated, result = await context.compactor.compact(
            context.checkpoint,
            context.context_plan.budget.model_copy(
                update={"history_tokens": pressure.history_budget_tokens}
            ),
            pressure.reason,
        )
        context.checkpoint = updated
        step.compactions += 1
        await _append_event(
            context,
            "context.compacted",
            {
                "step_number": step.step_number,
                "depth": result.depth,
                "source_event_ids": list(result.source_event_ids),
                "replaced_through_sequence": result.replaced_through_sequence,
                "tokens_before": result.tokens_before,
                "tokens_after": result.tokens_after,
                "compactor_version": result.compactor_version,
            },
        )
        await checkpoint(context, "compaction")


async def checkpoint(context: RunContext, trigger: str) -> None:
    """Advance the materialized M1 checkpoint and record the checkpoint event."""

    previous = context.checkpoint.model_copy(deep=True)
    context.checkpoint.budget_state = {
        "step_count": context.run.step_count,
        "model_call_count": context.run.model_call_count,
        "tool_call_count": context.run.tool_call_count,
        "usage": context.run.usage.model_dump(mode="json"),
        "context_model_id": (f"{context.resolved_model.provider}:{context.resolved_model.model}"),
        "context_seed_event_sequence": context.run.seed_event_sequence,
    }
    context.checkpoint.version += 1
    context.checkpoint.status = context.run.status
    context.checkpoint.created_at = context.clock.now()
    full = (
        context.checkpoint.version == 1
        or context.checkpoint.version % 8 == 1
        or trigger in {"compaction", "suspended", "cancelled", "failed", "final"}
    )
    try:
        async with context.uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=context.run.session_id,
                    run_id=context.run.id,
                    event_type="run.checkpointed",
                    actor_type="runtime",
                    payload={
                        "version": context.checkpoint.version,
                        "trigger": trigger,
                        "full": full,
                    },
                ),
                lease=context.lease,
            )
            context.checkpoint.last_event_sequence = event.sequence
            await uow.checkpoints.write(
                context.run.id,
                context.checkpoint,
                full=full,
                lease=context.lease,
            )
    except BaseException:
        context.checkpoint = previous
        raise


def _failure(
    context: RunContext,
    reason: FailureReason,
    error_class: str,
    message: str,
    step: Step | None,
) -> RunOutcome:
    return RunOutcome(
        kind=OutcomeKind.FAILED,
        failure=RunFailure(
            reason=reason,
            error_class=error_class,
            message=message,
            step_number=None if step is None else step.step_number,
            attempt_number=None if step is None else step.attempt_count,
            occurred_at=context.clock.now(),
        ),
    )


def select_final_message(turn: ModelTurn) -> AssistantMessage | None:
    if not turn.assistant_messages:
        return None
    return turn.assistant_messages[-1]


def _has_final_text(message: AssistantMessage | None) -> bool:
    return message is not None and any(
        isinstance(part, TextPart) and part.text for part in message.content
    )


def _denied_outcome(result: ToolResultItem) -> ToolOutcome | None:
    for part in result.content:
        if not isinstance(part, TextPart):
            continue
        try:
            outcome = ToolOutcome.model_validate_json(part.text)
        except ValueError:
            continue
        if outcome.status is ToolOutcomeStatus.DENIED:
            return outcome
    return None


def _reconcile_context_estimate(
    context: RunContext, request: ModelRequest, usage: ModelUsage
) -> None:
    raw_total = request.metadata.get("context_total_tokens")
    raw_reserve = request.metadata.get("context_reserve_tokens", "0")
    if raw_total is None or usage.input_tokens <= 0:
        return
    try:
        estimated = max(1, int(raw_total) - int(raw_reserve))
    except (TypeError, ValueError):
        return
    context.token_estimator.reconcile(
        f"{context.resolved_model.provider}:{context.resolved_model.model}",
        estimated,
        usage.input_tokens,
    )


async def _record_denials(
    context: RunContext,
    step: Step,
    calls: list[ToolCallItem],
    results: list[ToolResultItem],
) -> bool:
    denied = [
        (call, outcome)
        for call, result in zip(calls, results, strict=True)
        if (outcome := _denied_outcome(result)) is not None
    ]
    if not denied:
        return False
    async with context.uow_factory() as uow:
        invocations = await uow.invocations.list_for_run(context.run.id, context.principal)
    hashes = {
        invocation.call_id: invocation.normalized_arguments_hash
        for invocation in invocations
        if invocation.step_number == step.step_number
        and invocation.normalized_arguments_hash is not None
    }
    counters = context.checkpoint.working_state.setdefault("identical_denials", {})
    if not isinstance(counters, dict):
        raise RuntimeError("the identical-denial counter was malformed")
    tripped = False
    for call, outcome in denied:
        arguments_hash = hashes.get(call.call_id)
        if arguments_hash is None:
            canonical = json.dumps(
                call.arguments,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            arguments_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        key = json.dumps([call.name, arguments_hash, outcome.reason_code], separators=(",", ":"))
        count = int(counters.get(key, 0)) + 1
        counters[key] = count
        tripped = tripped or count >= context.identical_denial_threshold
    return tripped


async def _invoke_model(
    context: RunContext, step: Step, request: ModelRequest
) -> ModelTurn | RunOutcome:
    while step.attempt_count < context.max_internal_attempts:
        context.budgets.check(context.run, BudgetScope.ATTEMPT)
        step.attempt_count += 1
        attempt = ModelAttempt(
            attempt_id=context.ids.new_id(),
            run_id=context.run.id,
            step_number=step.step_number,
            attempt_number=step.attempt_count,
            started_at=context.clock.now(),
        )
        await _append_event(
            context,
            "model.request.started",
            {
                "attempt_id": str(attempt.attempt_id),
                "step_number": step.step_number,
                "prefix_sha256": request.metadata.get("prefix_sha256"),
                "context_epoch": request.metadata.get("context_epoch"),
                "context_total_tokens": request.metadata.get("context_total_tokens"),
                "context_capacity_tokens": request.metadata.get("context_capacity_tokens"),
                "context_reserve_tokens": request.metadata.get("context_reserve_tokens"),
            },
        )
        expected_sequence = 0
        terminal: ModelCompletedEvent | ModelFailedEvent | None = None
        if context.uow_factory.is_open():
            raise RuntimeError("model I/O cannot begin while a unit of work is open")
        try:
            stream = cast(
                AsyncGenerator[ModelEvent, None],
                context.model_provider.stream(request, context.resolved_model, attempt),
            )
            async with aclosing(stream):
                async for event in validated_stream(stream):
                    if event.sequence != expected_sequence:
                        return _failure(
                            context,
                            FailureReason.MODEL_PERMANENT_ERROR,
                            "ModelProtocolError",
                            "the normalized model stream had a sequence gap",
                            step,
                        )
                    expected_sequence += 1
                    if context.on_model_event is not None:
                        # This callback is on the provider-consumption path and must
                        # return promptly; it must never perform unbounded I/O.
                        await context.on_model_event(context.run, event)
                    if isinstance(event, (ModelCompletedEvent, ModelFailedEvent)):
                        if terminal is not None:
                            return _failure(
                                context,
                                FailureReason.MODEL_PERMANENT_ERROR,
                                "ModelProtocolError",
                                "the normalized model stream had multiple terminal events",
                                step,
                            )
                        terminal = event
        except ModelStreamError:
            return _failure(
                context,
                FailureReason.MODEL_PERMANENT_ERROR,
                "ModelProtocolError",
                "the normalized model stream violated its contract",
                step,
            )
        if terminal is None:
            return _failure(
                context,
                FailureReason.MODEL_PERMANENT_ERROR,
                "ModelProtocolError",
                "the normalized model stream ended without a terminal event",
                step,
            )
        if isinstance(terminal, ModelFailedEvent):
            await _append_event(
                context,
                "model.response.failed",
                {
                    "attempt_id": str(attempt.attempt_id),
                    "step_number": step.step_number,
                    "error_class": type(terminal.error).__name__,
                },
            )
            failure_usage = (
                terminal.partial_turn.usage
                if terminal.partial_turn is not None
                else ModelUsage(provider=terminal.error.provider, model=terminal.error.model)
            )
            await context.budgets.record_model_usage(
                context.run,
                failure_usage,
                step=step,
                attempt=attempt,
                request=request,
                resolved_model=context.resolved_model,
                model_turn=terminal.partial_turn,
                registry_version=(
                    None
                    if context.checkpoint.provider_pin is None
                    else context.checkpoint.provider_pin.registry_version
                ),
                error_kind=(
                    "transient" if isinstance(terminal.error, ModelTransientError) else "permanent"
                ),
            )
            _reconcile_context_estimate(context, request, failure_usage)
            if (
                isinstance(terminal.error, ModelTransientError)
                and not terminal.error.stream_had_output
                and step.attempt_count < context.max_internal_attempts
            ):
                continue
            reason = (
                FailureReason.MAX_ATTEMPTS_EXCEEDED
                if isinstance(terminal.error, ModelTransientError)
                else FailureReason.MODEL_PERMANENT_ERROR
            )
            return _failure(
                context,
                reason,
                type(terminal.error).__name__,
                terminal.error.message,
                step,
            )
        await _append_event(
            context,
            "model.response.completed",
            {
                "attempt_id": str(attempt.attempt_id),
                "step_number": step.step_number,
                "stop_reason": terminal.stop_reason.value,
                "tool_names": [call.name for call in terminal.turn.tool_calls],
                "conversation_items": [
                    item.model_dump(mode="json")
                    for item in [
                        *(terminal.turn.assistant_messages if terminal.turn.tool_calls else []),
                        *terminal.turn.tool_calls,
                    ]
                ],
            },
        )
        await context.budgets.record_model_usage(
            context.run,
            terminal.turn.usage,
            step=step,
            attempt=attempt,
            request=request,
            resolved_model=context.resolved_model,
            model_turn=terminal.turn,
            registry_version=(
                None
                if context.checkpoint.provider_pin is None
                else context.checkpoint.provider_pin.registry_version
            ),
            stop_reason=terminal.stop_reason,
        )
        _reconcile_context_estimate(context, request, terminal.turn.usage)
        if not terminal.turn.tool_calls and not _has_final_text(
            select_final_message(terminal.turn)
        ):
            if step.attempt_count < context.max_internal_attempts:
                continue
            return _failure(
                context,
                FailureReason.EMPTY_MODEL_TURN,
                "EmptyModelTurn",
                "the model produced neither a message nor tool calls",
                step,
            )
        return terminal.turn
    return _failure(
        context,
        FailureReason.MAX_ATTEMPTS_EXCEEDED,
        "ModelTransientError",
        "the model attempt limit was reached",
        step,
    )


async def run_loop(context: RunContext) -> RunOutcome:
    """Run model/tool steps until a final answer, failure, or cancellation."""

    while True:
        context.token.raise_if_cancelled()
        context.budgets.check(context.run, BudgetScope.STEP)
        context.run.step_count += 1
        context.run.updated_at = context.clock.now()
        async with context.uow_factory() as uow:
            await uow.runs.update_counters(context.run, lease=context.lease)
        step = Step(
            run_id=context.run.id,
            step_number=context.run.step_count,
            started_at=context.clock.now(),
        )
        request = await build_with_pressure(context, step)
        invoked = await _invoke_model(context, step, request)
        if isinstance(invoked, RunOutcome):
            return invoked
        turn = invoked
        if turn.stop_reason is StopReason.CANCELLED:
            return RunOutcome(kind=OutcomeKind.CANCELLED)
        context.token.raise_if_cancelled()
        if turn.tool_calls and turn.provider_reasoning_items:
            context.checkpoint.provider_continuation = ProviderContinuation(
                provider=context.resolved_model.provider,
                # The continuation is the provider-signed opaque reasoning block.
                # Provider response identifiers remain telemetry, not runtime input.
                previous_response_id=None,
                opaque_items=[
                    item.model_dump(mode="json") for item in turn.provider_reasoning_items
                ],
            )
        else:
            context.checkpoint.provider_continuation = None
        context.checkpoint.conversation.extend(turn.assistant_messages)
        context.checkpoint.conversation.extend(turn.tool_calls)
        await checkpoint(context, "model_response")

        if not turn.tool_calls:
            message = select_final_message(turn)
            if not _has_final_text(message):
                return _failure(
                    context,
                    FailureReason.EMPTY_MODEL_TURN,
                    "EmptyModelTurn",
                    "the model produced neither a message nor tool calls",
                    step,
                )
            assert message is not None
            await _append_event(
                context,
                "assistant.message.completed",
                {"message": message.model_dump(mode="json")},
            )
            return RunOutcome(kind=OutcomeKind.COMPLETED, final_message=message)

        call_counts = context.checkpoint.working_state.setdefault("identical_calls", {})
        if not isinstance(call_counts, dict):
            return _failure(
                context,
                FailureReason.INTERNAL_ERROR,
                "WorkingStateError",
                "the identical-call counter was malformed",
                step,
            )
        for call in turn.tool_calls:
            fingerprint = f"{call.name}:{call.raw_arguments}"
            count = int(call_counts.get(fingerprint, 0)) + 1
            call_counts[fingerprint] = count
            if count >= context.identical_call_threshold:
                return _failure(
                    context,
                    FailureReason.TOOL_LOOP_DETECTED,
                    "ToolLoopDetected",
                    "the model repeated an identical tool call "
                    f"{context.identical_call_threshold} times",
                    step,
                )

        context.checkpoint.pending_tool_calls = [
            call.model_dump(mode="json") for call in turn.tool_calls
        ]
        await checkpoint(context, "tool_pending")
        if context.uow_factory.is_open():
            raise RuntimeError("tool I/O cannot begin while a unit of work is open")
        try:
            results = await context.dispatch_tools(
                run=context.run,
                checkpoint=context.checkpoint,
                tool_calls=turn.tool_calls,
                principal=context.principal,
                step=step,
                agent=context.agent,
                token=context.token,
                lease=context.lease,
            )
        except ApprovalRequiredError as exc:
            if exc.approval_id not in context.checkpoint.pending_approval_ids:
                context.checkpoint.pending_approval_ids.append(exc.approval_id)
            await checkpoint(context, "suspended")
            return RunOutcome(
                kind=OutcomeKind.SUSPENDED,
                suspension={
                    "kind": "approval",
                    "approval_id": str(exc.approval_id),
                },
            )
        except UserInputRequiredError as exc:
            question = next(
                (
                    str(call.arguments.get("question"))
                    for call in turn.tool_calls
                    if call.name == "conversation.ask_user"
                    and isinstance(call.arguments.get("question"), str)
                ),
                "The run is waiting for user input.",
            )
            updated_state = _record_open_question(
                context.checkpoint,
                question,
                context.add_open_question,
            )
            context.checkpoint.working_state["outstanding_question_id"] = str(exc.question_id)
            await _append_event(
                context,
                "context.working_state.updated",
                {
                    "working_state": updated_state.model_dump(mode="json"),
                    "source": "runtime_question",
                },
            )
            await checkpoint(context, "suspended")
            return RunOutcome(
                kind=OutcomeKind.SUSPENDED,
                suspension={
                    "kind": "user",
                    "question_id": str(exc.question_id),
                    "invocation_id": str(exc.invocation_id),
                },
            )
        step.tool_call_count = len(results)
        await context.budgets.record_tool_usage(context.run, len(results), step=step)
        context.checkpoint.conversation.extend(results)
        context.checkpoint.pending_tool_calls = []
        repeated_denial = await _record_denials(context, step, turn.tool_calls, results)
        await checkpoint(context, "tool_call")
        if repeated_denial:
            return _failure(
                context,
                FailureReason.REPEATED_DENIAL,
                "ToolPolicyDenied",
                "the model repeated an identical denied action too many times",
                step,
            )
