"""The provider-neutral run loop; it computes an outcome and ends no run."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Protocol, cast

from agent_core.domain.agents import AgentSpec, Principal
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
    ToolResultItem,
)
from agent_core.domain.runs import (
    BudgetScope,
    FailureReason,
    OutcomeKind,
    Run,
    RunCheckpoint,
    RunFailure,
    RunOutcome,
    Step,
)
from agent_core.ports.context import ContextBuilder
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import CancellationToken
from agent_core.ports.events import EventRepository
from agent_core.ports.models import ModelProvider
from agent_core.ports.repositories import BudgetLedger, RunRepository

type ToolDispatch = Callable[..., Awaitable[list[ToolResultItem]]]


@dataclass(slots=True)
class RunContext:
    run: Run
    checkpoint: RunCheckpoint
    agent: AgentSpec
    principal: Principal
    context_builder: ContextBuilder
    model_provider: ModelProvider
    resolved_model: ResolvedModel
    budgets: BudgetLedger
    runs: RunRepository
    events: EventRepository
    clock: Clock
    ids: IdFactory
    token: CancellationToken
    dispatch_tools: ToolDispatch
    max_internal_attempts: int = 3
    identical_call_threshold: int = 5


class CheckpointContext(Protocol):
    run: Run
    checkpoint: RunCheckpoint
    events: EventRepository
    clock: Clock


async def _append_event(
    context: CheckpointContext, event_type: str, payload: dict[str, Any] | None = None
) -> None:
    await context.events.append(
        NewEvent(
            session_id=context.run.session_id,
            run_id=context.run.id,
            event_type=event_type,
            actor_type="runtime",
            payload=payload or {},
        )
    )


async def checkpoint(context: CheckpointContext, trigger: str) -> None:
    """Advance the materialized M1 checkpoint and record the checkpoint event."""

    context.checkpoint.version += 1
    context.checkpoint.status = context.run.status
    context.checkpoint.created_at = context.clock.now()
    await _append_event(
        context,
        "run.checkpointed",
        {"version": context.checkpoint.version, "trigger": trigger},
    )


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
            },
        )
        expected_sequence = 0
        terminal: ModelCompletedEvent | ModelFailedEvent | None = None
        stream = cast(
            AsyncGenerator[ModelEvent, None],
            context.model_provider.stream(request, context.resolved_model, attempt),
        )
        async with aclosing(stream):
            async for event in stream:
                if event.sequence != expected_sequence:
                    return _failure(
                        context,
                        FailureReason.MODEL_PERMANENT_ERROR,
                        "ModelProtocolError",
                        "the normalized model stream had a sequence gap",
                        step,
                    )
                expected_sequence += 1
                if terminal is not None:
                    return _failure(
                        context,
                        FailureReason.MODEL_PERMANENT_ERROR,
                        "ModelProtocolError",
                        "the normalized model stream continued after its terminal event",
                        step,
                    )
                if isinstance(event, (ModelCompletedEvent, ModelFailedEvent)):
                    terminal = event
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
            await context.budgets.record_model_usage(
                context.run,
                terminal.partial_turn.usage
                if terminal.partial_turn is not None
                else ModelUsage(provider=terminal.error.provider, model=terminal.error.model),
                step=step,
            )
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
            },
        )
        if not terminal.turn.tool_calls and not _has_final_text(
            select_final_message(terminal.turn)
        ):
            await context.budgets.record_model_usage(context.run, terminal.turn.usage, step=step)
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
        await context.runs.update_counters(context.run)
        step = Step(
            run_id=context.run.id,
            step_number=context.run.step_count,
            started_at=context.clock.now(),
        )
        request = await context.context_builder.build(
            context.run, context.checkpoint, context.agent, context.principal
        )
        invoked = await _invoke_model(context, step, request)
        if isinstance(invoked, RunOutcome):
            return invoked
        turn = invoked
        if turn.stop_reason is StopReason.CANCELLED:
            return RunOutcome(kind=OutcomeKind.CANCELLED)
        await context.budgets.record_model_usage(context.run, turn.usage, step=step)
        context.token.raise_if_cancelled()
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
            await checkpoint(context, "final")
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

        results = await context.dispatch_tools(
            run=context.run,
            checkpoint=context.checkpoint,
            tool_calls=turn.tool_calls,
            principal=context.principal,
            step=step,
            agent=context.agent,
            token=context.token,
        )
        step.tool_call_count = len(results)
        await context.budgets.record_tool_usage(context.run, len(results), step=step)
        for result in results:
            context.checkpoint.conversation.append(result)
            await checkpoint(context, "tool_call")
