"""The provider-neutral run loop; it computes an outcome and ends no run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.artifacts import reply_attachments, reply_file_reference
from agent_core.domain.context import ContextPlan, WorkingState
from agent_core.domain.errors import (
    ApprovalRequiredError,
    ChildRunRequiredError,
    ContextOverflow,
    UserInputRequiredError,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    AssistantMessage,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelFailure,
    ModelRequest,
    ModelTransientError,
    ModelTurn,
    ModelUsage,
    ReasoningEffort,
    ResolvedModel,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.policies import TrustLevel
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
from agent_core.ports.notifications import RunNotificationProducer
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
    notification_producer: RunNotificationProducer | None = None
    finalization_write_probe: Callable[[str], None] | None = None
    on_model_event: ModelEventCallback | None = None
    max_internal_attempts: int = 3
    identical_call_threshold: int = 5
    identical_denial_threshold: int = 3
    max_compactions_per_step: int = 2
    # ADR-0119: the owner's chat effort, when this run's model accepts it.
    reasoning_effort: ReasoningEffort | None = None


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


async def _complete_reply(context: RunContext, message: AssistantMessage) -> AssistantMessage:
    """Record the final reply with every file the run exported (ADR-0122).

    One unit of work chooses the files, keeps them for the life of the
    conversation, and records the reply, so a fenced lease or an erasure fence
    rolls all three back together. The returned object is also the run's final
    message: `run.completed` repeats it, and a client that compares the two
    must see the same content.
    """

    reply = message
    async with context.uow_factory() as uow:
        exported = reply_attachments(
            await uow.artifacts.list_for_run(context.run.id, context.principal)
        )
        if exported:
            retained = await uow.artifacts.retain_for_reply(
                [artifact.id for artifact in exported],
                context.principal,
                run_id=context.run.id,
            )
            reply = message.model_copy(
                update={
                    "content": [
                        *message.content,
                        *(reply_file_reference(artifact) for artifact in retained),
                    ]
                },
                deep=True,
            )
        await uow.events.append(
            NewEvent(
                session_id=context.run.session_id,
                run_id=context.run.id,
                event_type="assistant.message.completed",
                actor_type="runtime",
                payload={"message": reply.model_dump(mode="json")},
            ),
            lease=context.lease,
        )
    # Only a committed reply replaces the turn's copy in the checkpoint.
    conversation = context.checkpoint.conversation
    for index, item in enumerate(conversation):
        if item is message:
            conversation[index] = reply
    return reply


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


def _apply_context_origin_trust(checkpoint_state: RunCheckpoint, request: ModelRequest) -> None:
    try:
        checkpoint_state.context_origin_trust = TrustLevel(
            request.metadata.get("context_origin_trust", TrustLevel.USER.value)
        )
    except ValueError as exc:
        raise ContextOverflow("context builder returned an invalid trust marker") from exc


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


def _elapsed_ms(started_at: datetime, finished_at: datetime | None) -> int | None:
    """Whole milliseconds between two clock readings, or None when the second never came."""
    if finished_at is None:
        return None
    return max(0, round((finished_at - started_at).total_seconds() * 1000))


def _failure(
    context: RunContext,
    reason: FailureReason,
    error_class: str,
    message: str,
    step: Step | None,
    details: dict[str, Any] | None = None,
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
            details={} if details is None else details,
        ),
    )


def _provider_failure_details(error: ModelFailure) -> dict[str, Any]:
    details: dict[str, Any] = {"provider": error.provider}
    if error.provider_code is not None:
        details["provider_code"] = error.provider_code
    if error.http_status is not None:
        details["http_status"] = error.http_status
    if error.provider_parameter is not None:
        details["provider_parameter"] = error.provider_parameter
    return details


def select_final_message(turn: ModelTurn) -> AssistantMessage | None:
    if not turn.assistant_messages:
        return None
    return turn.assistant_messages[-1]


def _has_final_text(message: AssistantMessage | None) -> bool:
    return message is not None and any(
        isinstance(part, TextPart) and part.text for part in message.content
    )


# ADR-0130: how many times a run's identical-call counts may restart on new
# evidence, across all calls.
MAXIMUM_EVIDENCE_RESETS = 32


def apply_tool_evidence(working_state: dict[str, Any], calls: Sequence[ToolCallItem]) -> None:
    """ADR-0130: an identical call that observed something new restarts its
    count, at most MAXIMUM_EVIDENCE_RESETS times a run."""

    evidence = working_state.pop("tool_evidence", None)
    if not isinstance(evidence, dict):
        return
    counts = working_state.get("identical_calls")
    recorded = working_state.setdefault("identical_call_evidence", {})
    if not isinstance(counts, dict) or not isinstance(recorded, dict):
        return
    for call in calls:
        key = evidence.get(call.call_id)
        if not isinstance(key, str):
            continue
        fingerprint = f"{call.name}:{call.raw_arguments}"
        previous = recorded.get(fingerprint)
        recorded[fingerprint] = key
        if previous is None or previous == key:
            continue
        resets = int(working_state.get("identical_call_resets", 0))
        if int(counts.get(fingerprint, 0)) > 1 and resets < MAXIMUM_EVIDENCE_RESETS:
            counts[fingerprint] = 1
            working_state["identical_call_resets"] = resets + 1


def _synthesis_reserve_dimension(
    run: Run,
    *,
    step_in_progress: bool = False,
) -> str | None:
    """Name the first run budget whose final-synthesis reserve began."""

    limits = run.limits
    remaining_tool_calls = limits.max_tool_calls - run.tool_call_count
    if remaining_tool_calls <= 0:
        # Exhaustion is not a reserve. Even all-zero reserves owe the model one
        # tool-free turn to answer from the evidence it already gathered.
        return "tool_calls"
    if not (
        limits.synthesis_reserve_steps
        or limits.synthesis_reserve_model_calls
        or limits.synthesis_reserve_tool_calls
        or limits.synthesis_reserve_cost
    ):
        return None
    remaining_steps = limits.max_steps - run.step_count + int(step_in_progress)
    if remaining_steps <= limits.synthesis_reserve_steps:
        return "steps"
    if limits.max_model_calls - run.model_call_count <= limits.synthesis_reserve_model_calls:
        return "model_calls"
    if remaining_tool_calls <= limits.synthesis_reserve_tool_calls:
        return "tool_calls"
    if limits.max_cost is not None and (
        limits.max_cost - run.usage.cost <= limits.synthesis_reserve_cost
    ):
        return "cost"
    return None


def _synthesis_only_request(request: ModelRequest, dimension: str) -> ModelRequest:
    """Add a volatile platform control that protects final-synthesis headroom."""

    control = UserMessage(
        content=[
            TextPart(
                text=(
                    "Runtime control: the final-synthesis reserve is active "
                    f"because the run {dimension} budget is exhausted. Do not call "
                    "tools. Synthesize the best-supported final answer from evidence "
                    "already in the conversation and state any remaining gap."
                )
            )
        ],
        trust=TrustLevel.PLATFORM,
        principal_id=None,
    )
    return request.model_copy(
        update={"conversation": [*request.conversation, control]},
        deep=True,
    )


def _fit_tool_batch(
    run: Run, calls: list[ToolCallItem]
) -> tuple[list[ToolCallItem], list[ToolResultItem]]:
    """Split a batch into the calls that fit the tool-call budget and refusals.

    A refused call never reaches the tool executor, so nothing runs
    unaccounted for; its result tells the model to answer from what it has.
    """

    remaining = max(0, run.limits.max_tool_calls - run.tool_call_count)
    fitted = calls[:remaining]
    refused: list[ToolResultItem] = []
    for call in calls[remaining:]:
        outcome = ToolOutcome(
            status=ToolOutcomeStatus.FAILED,
            action=call.name,
            reason_code="tool.budget_exhausted",
            message=(
                "Not performed. The run's tool-call budget is exhausted; "
                "answer from the evidence already gathered."
            ),
            retryable=False,
            remediation="none",
        )
        refused.append(
            ToolResultItem(
                call_id=call.call_id,
                content=[TextPart(text=outcome.model_dump_json())],
                is_error=True,
                trust=TrustLevel.PLATFORM,
            )
        )
    return fitted, refused


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
    context: RunContext,
    step: Step,
    request: ModelRequest,
    initial_synthesis_reserve: str | None,
    *,
    retain_response: bool = True,
) -> ModelTurn | RunOutcome:
    """Consume one bounded model attempt sequence and persist authoritative usage.

    A caller that validates the turn itself and must not store what it rejects
    passes ``retain_response=False``: the response event then keeps its tool
    names and stop reason but none of the returned text or arguments.
    """

    while step.attempt_count < context.max_internal_attempts:
        context.budgets.check(context.run, BudgetScope.ATTEMPT)
        synthesis_reserve = initial_synthesis_reserve or _synthesis_reserve_dimension(
            context.run,
            step_in_progress=True,
        )
        attempt_request = (
            _synthesis_only_request(request, synthesis_reserve)
            if synthesis_reserve is not None
            else request
        )
        if context.reasoning_effort is not None and attempt_request.reasoning_effort is None:
            attempt_request = attempt_request.model_copy(
                update={"reasoning_effort": context.reasoning_effort}
            )
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
                "prefix_sha256": attempt_request.metadata.get("prefix_sha256"),
                "context_epoch": attempt_request.metadata.get("context_epoch"),
                "context_total_tokens": attempt_request.metadata.get("context_total_tokens"),
                "context_capacity_tokens": attempt_request.metadata.get("context_capacity_tokens"),
                "context_reserve_tokens": attempt_request.metadata.get("context_reserve_tokens"),
                "reasoning_effort": (
                    None
                    if attempt_request.reasoning_effort is None
                    else attempt_request.reasoning_effort.value
                ),
            },
        )
        expected_sequence = 0
        terminal: ModelCompletedEvent | ModelFailedEvent | None = None
        if context.uow_factory.is_open():
            raise RuntimeError("model I/O cannot begin while a unit of work is open")
        # Timed on the run's clock from the moment the request is issued (ADR-0131).
        issued_at = context.clock.now()
        first_event_at: datetime | None = None
        first_text_at: datetime | None = None
        terminal_at = issued_at
        try:
            stream = cast(
                AsyncGenerator[ModelEvent, None],
                context.model_provider.stream(
                    attempt_request,
                    context.resolved_model,
                    attempt,
                ),
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
                    observed_at = context.clock.now()
                    if first_event_at is None:
                        first_event_at = observed_at
                    if first_text_at is None and isinstance(event, TextDeltaEvent) and event.text:
                        first_text_at = observed_at
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
                        terminal_at = observed_at
        except ModelStreamError as exc:
            return _failure(
                context,
                FailureReason.MODEL_PERMANENT_ERROR,
                "ModelProtocolError",
                "the normalized model stream violated its contract",
                step,
                {"protocol_detail": str(exc)},
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
            provider_details = _provider_failure_details(terminal.error)
            await _append_event(
                context,
                "model.response.failed",
                {
                    "attempt_id": str(attempt.attempt_id),
                    "step_number": step.step_number,
                    "error_class": type(terminal.error).__name__,
                    **provider_details,
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
                request=attempt_request,
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
            _reconcile_context_estimate(context, attempt_request, failure_usage)
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
                provider_details,
            )
        await _append_event(
            context,
            "model.response.completed",
            {
                "attempt_id": str(attempt.attempt_id),
                "step_number": step.step_number,
                "stop_reason": terminal.stop_reason.value,
                "usage": terminal.turn.usage.model_dump(mode="json"),
                "internal_retry_count": terminal.internal_retry_count,
                "duration_ms": _elapsed_ms(issued_at, terminal_at),
                "time_to_first_event_ms": _elapsed_ms(issued_at, first_event_at),
                "time_to_first_text_ms": _elapsed_ms(issued_at, first_text_at),
                "tool_names": [call.name for call in terminal.turn.tool_calls],
                "conversation_items": [
                    item.model_dump(mode="json")
                    for item in [
                        *(terminal.turn.assistant_messages if terminal.turn.tool_calls else []),
                        *terminal.turn.tool_calls,
                    ]
                ]
                if retain_response
                else [],
            },
        )
        await context.budgets.record_model_usage(
            context.run,
            terminal.turn.usage,
            step=step,
            attempt=attempt,
            request=attempt_request,
            resolved_model=context.resolved_model,
            model_turn=terminal.turn,
            registry_version=(
                None
                if context.checkpoint.provider_pin is None
                else context.checkpoint.provider_pin.registry_version
            ),
            stop_reason=terminal.stop_reason,
        )
        _reconcile_context_estimate(context, attempt_request, terminal.turn.usage)
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
        synthesis_reserve = _synthesis_reserve_dimension(context.run)
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
        _apply_context_origin_trust(context.checkpoint, request)
        invoked = await _invoke_model(
            context,
            step,
            request,
            synthesis_reserve,
        )
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
            message = await _complete_reply(context, message)
            return RunOutcome(kind=OutcomeKind.COMPLETED, final_message=message)

        synthesis_reserve = _synthesis_reserve_dimension(
            context.run,
            step_in_progress=True,
        )
        if synthesis_reserve is not None:
            return _failure(
                context,
                FailureReason.BUDGET_EXCEEDED,
                "SynthesisReserveViolation",
                "the model requested another tool inside its final synthesis reserve",
                step,
                {"synthesis_reserve": synthesis_reserve},
            )

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

        # Fit the batch to the remaining budget before anything runs. Refusals
        # join the conversation now, so a resumed step re-dispatches only the
        # calls that fit and the model still receives a result for every call.
        fitted_calls, refused_results = _fit_tool_batch(context.run, turn.tool_calls)
        context.checkpoint.conversation.extend(refused_results)
        context.checkpoint.pending_tool_calls = [
            call.model_dump(mode="json") for call in fitted_calls
        ]
        if not fitted_calls:
            await checkpoint(context, "tool_call")
            continue
        await checkpoint(context, "tool_pending")
        if context.uow_factory.is_open():
            raise RuntimeError("tool I/O cannot begin while a unit of work is open")
        try:
            results = await context.dispatch_tools(
                run=context.run,
                checkpoint=context.checkpoint,
                tool_calls=fitted_calls,
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
        except ChildRunRequiredError as exc:
            await checkpoint(context, "suspended")
            return RunOutcome(
                kind=OutcomeKind.SUSPENDED,
                suspension={
                    "kind": "child_run",
                    "delegation_id": str(exc.delegation_id),
                    "invocation_id": str(exc.invocation_id),
                    "child_run_ids": [str(child_id) for child_id in exc.child_run_ids],
                },
            )
        except UserInputRequiredError as exc:
            question = next(
                (
                    str(call.arguments.get("question"))
                    for call in fitted_calls
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
        apply_tool_evidence(context.checkpoint.working_state, fitted_calls)
        context.checkpoint.pending_tool_calls = []
        repeated_denial = await _record_denials(context, step, fitted_calls, results)
        await checkpoint(context, "tool_call")
        if repeated_denial:
            return _failure(
                context,
                FailureReason.REPEATED_DENIAL,
                "ToolPolicyDenied",
                "the model repeated an identical denied action too many times",
                step,
            )
