"""The provider-neutral run loop; it computes an outcome and ends no run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.artifacts import reply_attachments, reply_file_reference
from agent_core.domain.browser import (
    BROWSER_AUTH_INTERRUPTION_MARKER,
    BrowserAuthenticationWait,
    BrowserObservation,
    BrowserProfileStatus,
    browser_origin,
)
from agent_core.domain.context import ContextPlan, WorkingState
from agent_core.domain.errors import (
    ApprovalRequiredError,
    ChildRunRequiredError,
    ContextOverflow,
    NotFoundError,
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
from agent_core.domain.sessions import SESSION_BROWSER_PROFILE_METADATA_KEY
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
    # ADR-0144: a chat run asks for a reasoning summary its client can show.
    reasoning_summary: bool = False


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
        if pressure.yield_steps or not pressure.fits:
            await _append_event(
                context,
                "context.budget.pressure",
                {
                    "step_number": step.step_number,
                    "reason": pressure.reason,
                    "fits": pressure.fits,
                    "total_tokens": pressure.total_tokens,
                    "capacity_tokens": pressure.capacity_tokens,
                    "yield_steps": list(pressure.yield_steps),
                },
            )
        if pressure.fits:
            return assembled.request
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
                        **(
                            {"runtime_tool_call": context.checkpoint.pending_tool_calls[0]}
                            if trigger in {"browser_recovery", "browser_workflow"}
                            else {}
                        ),
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


def _loop_synthesis_needed(context: RunContext) -> bool:
    """Leave an answer opportunity before another unchanged call trips the breaker.

    Read the checkpoint counter after tool evidence has reset progressing calls,
    including a batch finished by approval or crash recovery.
    """

    counts = context.checkpoint.working_state.get("identical_calls", {})
    return isinstance(counts, dict) and any(
        int(count) >= context.identical_call_threshold - 1 for count in counts.values()
    )


def _synthesis_only_request(request: ModelRequest, dimension: str) -> ModelRequest:
    """Add a volatile platform control that protects final-synthesis headroom."""

    reason = (
        "Runtime control: repeated-tool synthesis is required because an identical "
        "tool call is one repetition away from the loop limit. Do not call tools. "
        "Synthesize the best-supported final answer from evidence already in the "
        "conversation, state any remaining gap, and explain that research stopped "
        "because it was repeating. Do not claim the unfinished work is complete."
        if dimension == "repeated_tool_calls"
        else (
            "Runtime control: the final-synthesis reserve is active "
            f"because the run {dimension} budget is exhausted. Do not call "
            "tools. Synthesize the best-supported final answer from evidence "
            "already in the conversation and state any remaining gap."
        )
    )
    control = UserMessage(
        content=[
            TextPart(
                text=(
                    "Runtime control: the reviewed browser workflow has stopped. Do not call tools "
                    "or repeat its actions. Report only what the existing evidence confirms, "
                    "and state any unverified outcome or remaining gap."
                    if dimension == "browser_workflow"
                    else reason
                )
            )
        ],
        trust=TrustLevel.PLATFORM,
        principal_id=None,
    )
    return request.model_copy(
        update={"conversation": [*request.conversation, control], "tool_choice": "none"},
        deep=True,
    )


def _fit_tool_batch(
    run: Run, calls: list[ToolCallItem], *, used: int | None = None
) -> tuple[list[ToolCallItem], list[tuple[ToolCallItem, ToolResultItem]]]:
    """Split a batch into the calls that fit the tool-call budget and refusals.

    A refused call never reaches the tool executor, so nothing runs
    unaccounted for; its result tells the model to answer from what it has.
    `used` is the tool-call count the batch was proposed under, for a resumed
    batch whose usage the run's own count may already include.
    """

    count = run.tool_call_count if used is None else used
    remaining = max(0, run.limits.max_tool_calls - count)
    fitted = calls[:remaining]
    refused: list[tuple[ToolCallItem, ToolResultItem]] = []
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
            (
                call,
                ToolResultItem(
                    call_id=call.call_id,
                    content=[TextPart(text=outcome.model_dump_json())],
                    is_error=True,
                    trust=TrustLevel.PLATFORM,
                ),
            )
        )
    return fitted, refused


async def _record_refusals(
    context: RunContext, refused: list[tuple[ToolCallItem, ToolResultItem]]
) -> None:
    """Append each budget refusal to the log, then to the conversation.

    A checkpoint stores its conversation as session history, which is built
    from events, so a refusal held only in memory leaves its call unanswered
    for every later reader of that history. A refusal is a pipeline failure,
    not a policy denial, so it is recorded as `tool.call.failed` in the shape
    of the tool pipeline's own refusals. It is not tool usage.
    """

    if not refused:
        return
    async with context.uow_factory() as uow:
        for call, result in refused:
            await uow.events.append(
                NewEvent(
                    session_id=context.run.session_id,
                    run_id=context.run.id,
                    event_type="tool.call.failed",
                    actor_type="runtime",
                    payload={
                        "name": call.name,
                        "call_id": call.call_id,
                        "reason_code": "tool.budget_exhausted",
                        "result_item": result.model_dump(mode="json"),
                    },
                ),
                lease=context.lease,
            )
    context.checkpoint.conversation.extend(result for _call, result in refused)


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


async def recover_browser_failure(
    context: RunContext, step: Step, calls: list[ToolCallItem], results: list[ToolResultItem]
) -> None:
    """Queue a separate bounded read or owner question, never an action replay."""
    failures: set[str] = set()
    for call, result in zip(calls, results, strict=True):
        if call.name not in {"browser.observe", "browser.navigate", "browser.act"}:
            continue
        if call.call_id != result.call_id:
            continue
        if not result.is_error:
            for part in result.content[:1]:
                if isinstance(part, TextPart):
                    try:
                        observed = BrowserObservation.model_validate_json(part.text)
                    except ValueError:
                        continue
                    if observed.interruption == "needs_user":
                        failures.add("tool.browser.needs_user")
            continue
        for part in result.content[:1]:
            if not isinstance(part, TextPart):
                continue
            try:
                outcome = ToolOutcome.model_validate_json(part.text)
            except ValueError:
                continue
            if outcome.action == call.name:
                failures.add(outcome.reason_code)
                if (
                    call.name == "browser.act"
                    and outcome.reason_code == "tool.browser.outcome_unknown"
                    and any(
                        isinstance(hint, TextPart) and hint.text == BROWSER_AUTH_INTERRUPTION_MARKER
                        for hint in result.content[1:]
                    )
                ):
                    failures.add("tool.browser.needs_user")
    available = set(context.context_plan.tool_names) | set(context.context_plan.deferred_tool_names)
    if context.run.tool_call_count >= context.run.limits.max_tool_calls:
        return
    needs_user = bool(
        failures & {"tool.browser.authentication_required", "tool.browser.needs_user"}
    )
    if needs_user:
        name, counter, maximum = "conversation.ask_user", "browser_auth_interruptions", 2
        wait = await prepare_browser_auth_wait(context)
        if wait is not None:
            episode = f"{wait.profile_id}:{wait.generation}"
            if context.checkpoint.working_state.get("browser_auth_episode") == episode:
                return
            context.checkpoint.working_state["browser_auth_episode"] = episode
            context.checkpoint.working_state["browser_auth_wait"] = wait.model_dump(mode="json")
        arguments: dict[str, Any] = {
            "question": (
                "Please sign in to this chat's website in Website Access on your device, "
                + (
                    "and I will continue automatically once sign-in is verified. "
                    if wait
                    else "then reply here to continue. "
                )
                + "Keep passwords, verification codes and CAPTCHA "
                "answers in the sign-in window. I will read fresh evidence before continuing; "
                "the interrupted operation will not be replayed."
            )
        }
    elif failures & {"tool.browser.page_changed", "tool.browser.element_not_found"}:
        if any(call.call_id.startswith("browser-recovery:") for call in calls):
            return
        name, counter, maximum = "browser.observe", "browser_recovery_reads", 3
        arguments = {}
    else:
        return
    if name not in available:
        return
    count = context.checkpoint.working_state.get(counter, 0)
    if type(count) is not int or not 0 <= count < maximum:
        return
    context.checkpoint.working_state[counter] = count + 1
    call = ToolCallItem(
        call_id=f"browser-recovery:{context.ids.new_id()}",
        item_index=0,
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )
    context.checkpoint.conversation.append(call)
    context.checkpoint.pending_tool_calls = [call.model_dump(mode="json")]
    await checkpoint(context, "browser_recovery")
    recovered = await context.dispatch_tools(
        run=context.run,
        checkpoint=context.checkpoint,
        tool_calls=[call],
        principal=context.principal,
        step=step,
        agent=context.agent,
        token=context.token,
        lease=context.lease,
    )
    await context.budgets.record_tool_usage(context.run, len(recovered), step=step)
    context.checkpoint.conversation.extend(recovered)
    apply_tool_evidence(context.checkpoint.working_state, [call])
    context.checkpoint.pending_tool_calls = []
    # Only an authentication refusal from this read can lead to a question.
    # Its stale failures cannot lead to another recovery read.
    await recover_browser_failure(context, step, [call], recovered)


async def prepare_browser_auth_wait(context: RunContext) -> BrowserAuthenticationWait | None:
    try:
        async with context.uow_factory() as uow:
            session = await uow.sessions.get(context.run.session_id, context.principal)
            selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
            if not isinstance(selected, str):
                return None
            profile = await uow.browser_profiles.get(UUID(selected), context.principal)
    except (ValueError, NotFoundError):
        return None
    if profile.status in {BrowserProfileStatus.PROVISIONING, BrowserProfileStatus.REVOKED}:
        return None
    resume_url = profile.allowed_origins[0] + "/"
    for item in reversed(context.checkpoint.conversation):
        candidate = None
        if isinstance(item, ToolCallItem) and item.name == "browser.navigate":
            candidate = item.arguments.get("url")
        elif isinstance(item, ToolResultItem) and not item.is_error:
            for part in item.content[:1]:
                if isinstance(part, TextPart):
                    try:
                        observed = BrowserObservation.model_validate_json(part.text)
                        if observed.interruption is None:
                            candidate = observed.url
                    except ValueError:
                        pass
        if isinstance(candidate, str):
            try:
                if browser_origin(candidate) in profile.allowed_origins:
                    resume_url = candidate
                    break
            except ValueError:
                pass
    now = context.clock.now()
    return BrowserAuthenticationWait(
        profile_id=profile.id,
        generation=profile.generation,
        started_at=now,
        resume_url=resume_url,
        expires_at=min(
            now + timedelta(minutes=15), context.run.deadline_at or now + timedelta(minutes=15)
        ),
    )


def bind_browser_auth_question(checkpoint: RunCheckpoint, question_id: UUID) -> None:
    raw = checkpoint.working_state.get("browser_auth_wait")
    if raw is not None:
        wait = BrowserAuthenticationWait.model_validate(raw)
        checkpoint.working_state["browser_auth_wait"] = wait.model_copy(
            update={"question_id": question_id}
        ).model_dump(mode="json")


async def suspend_for_browser_question(
    context: RunContext, error: UserInputRequiredError
) -> RunOutcome:
    question = str(context.checkpoint.pending_tool_calls[0]["arguments"]["question"])
    state = _record_open_question(context.checkpoint, question, context.add_open_question)
    context.checkpoint.working_state["outstanding_question_id"] = str(error.question_id)
    bind_browser_auth_question(context.checkpoint, error.question_id)
    await _append_event(
        context,
        "context.working_state.updated",
        {
            "working_state": state.model_dump(mode="json"),
            "source": "runtime_question",
        },
    )
    await checkpoint(context, "suspended")
    return RunOutcome(
        kind=OutcomeKind.SUSPENDED,
        suspension={
            "kind": "user",
            "question_id": str(error.question_id),
            "invocation_id": str(error.invocation_id),
        },
    )


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
        if context.reasoning_summary and not attempt_request.reasoning_summary:
            attempt_request = attempt_request.model_copy(update={"reasoning_summary": True})
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
                # This survives checkpoint loss and distinguishes an answered
                # batch from one whose usage was already committed.
                "tool_call_count_before_turn": context.run.tool_call_count,
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


def guard_tool_turn(
    context: RunContext,
    calls: Sequence[ToolCallItem],
    step: Step,
    *,
    loop_synthesis: bool | None = None,
) -> RunOutcome | None:
    """Apply the same pre-dispatch guards to fresh and recovered model turns."""

    if loop_synthesis is None:
        loop_synthesis = _loop_synthesis_needed(context)
    synthesis_reserve = _synthesis_reserve_dimension(
        context.run,
        step_in_progress=True,
    )
    if context.checkpoint.working_state.get("browser_workflow_report_only"):
        synthesis_reserve = "browser_workflow"
    if synthesis_reserve is not None:
        return _failure(
            context,
            FailureReason.BUDGET_EXCEEDED,
            "SynthesisReserveViolation",
            "the model requested another tool inside its final synthesis reserve",
            step,
            {"synthesis_reserve": synthesis_reserve},
        )

    if loop_synthesis:
        return _failure(
            context,
            FailureReason.TOOL_LOOP_DETECTED,
            "ToolLoopDetected",
            "the model requested another tool during repeated-tool synthesis",
            step,
            {"identical_call_threshold": context.identical_call_threshold},
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
    for call in calls:
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

    return None


async def run_loop(context: RunContext) -> RunOutcome:
    """Run model/tool steps until a final answer, failure, or cancellation."""

    while True:
        context.token.raise_if_cancelled()
        synthesis_reserve = (
            "browser_workflow"
            if context.checkpoint.working_state.get("browser_workflow_report_only")
            else _synthesis_reserve_dimension(context.run)
        )
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
        loop_synthesis = _loop_synthesis_needed(context)
        invoked = await _invoke_model(
            context,
            step,
            request,
            synthesis_reserve or ("repeated_tool_calls" if loop_synthesis else None),
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

        guarded = guard_tool_turn(context, turn.tool_calls, step, loop_synthesis=loop_synthesis)
        if guarded is not None:
            return guarded

        # Fit the batch to the remaining budget before anything runs. Refusals
        # join the log and the conversation now, so a resumed step re-dispatches
        # only the calls that fit and the model still receives a result for
        # every call.
        fitted_calls, refusals = _fit_tool_batch(context.run, turn.tool_calls)
        await _record_refusals(context, refusals)
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
            bind_browser_auth_question(context.checkpoint, exc.question_id)
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
        try:
            await recover_browser_failure(context, step, fitted_calls, results)
        except UserInputRequiredError as exc:
            return await suspend_for_browser_question(context, exc)
        await checkpoint(context, "tool_call")
        if repeated_denial:
            return _failure(
                context,
                FailureReason.REPEATED_DENIAL,
                "ToolPolicyDenied",
                "the model repeated an identical denied action too many times",
                step,
            )
