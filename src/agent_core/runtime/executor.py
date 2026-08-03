"""Run execution and the only run-state transition writer."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import BudgetExceededError, RunCancelledError
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import AssistantMessage, ResolvedModel, TextPart, UserMessage
from agent_core.domain.runs import (
    FailureReason,
    OutcomeKind,
    Run,
    RunCheckpoint,
    RunFailure,
    RunOutcome,
    RunStatus,
)
from agent_core.ports.context import ContextBuilder
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.events import EventRepository
from agent_core.ports.models import ModelProvider
from agent_core.ports.repositories import (
    AgentRepository,
    BudgetLedger,
    PrincipalResolver,
    RunRepository,
)
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.loop import RunContext, ToolDispatch, checkpoint, run_loop

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _FinalizationContext:
    run: Run
    checkpoint: RunCheckpoint
    runs: RunRepository
    events: EventRepository
    clock: Clock
    token: RunCancellationToken


class RunExecutor:
    def __init__(
        self,
        *,
        principal: Principal,
        principals: PrincipalResolver,
        runs: RunRepository,
        agents: AgentRepository,
        events: EventRepository,
        context_builder: ContextBuilder,
        model_provider: ModelProvider,
        resolved_model: ResolvedModel,
        budgets: BudgetLedger,
        clock: Clock,
        ids: IdFactory,
        dispatch_tools: ToolDispatch,
        on_token: Callable[[RunCancellationToken], None] | None = None,
        max_internal_attempts: int = 3,
        identical_call_threshold: int = 5,
    ) -> None:
        self._principal = principal
        self._principals = principals
        self._runs = runs
        self._agents = agents
        self._events = events
        self._context_builder = context_builder
        self._model_provider = model_provider
        self._resolved_model = resolved_model
        self._budgets = budgets
        self._clock = clock
        self._ids = ids
        self._dispatch_tools = dispatch_tools
        self._on_token = on_token
        self._max_internal_attempts = max_internal_attempts
        self._identical_call_threshold = identical_call_threshold

    async def execute(self, run_id: UUID) -> None:
        run = await self._runs.get(run_id, self._principal)
        principal = await self._principals.for_run(run)
        run = await self._runs.transition(run.id, RunStatus.QUEUED, RunStatus.RUNNING)
        token = RunCancellationToken(self._clock, run.deadline_at)
        finalization = _FinalizationContext(
            run=run,
            runs=self._runs,
            events=self._events,
            clock=self._clock,
            token=token,
            checkpoint=RunCheckpoint(
                run_id=run.id,
                version=0,
                status=RunStatus.RUNNING,
                created_at=self._clock.now(),
            ),
        )
        context: RunContext | None = None
        try:
            await self._append(run, "run.started")
            finalization.checkpoint = await self._seed_checkpoint(run)
            agent = await self._agents.get_version(run.agent_id, run.agent_version)
            if self._on_token is not None:
                self._on_token(token)
            context = RunContext(
                run=run,
                checkpoint=finalization.checkpoint,
                agent=agent,
                principal=principal,
                context_builder=self._context_builder,
                model_provider=self._model_provider,
                resolved_model=self._resolved_model,
                budgets=self._budgets,
                runs=self._runs,
                events=self._events,
                clock=self._clock,
                ids=self._ids,
                token=token,
                dispatch_tools=self._dispatch_tools,
                max_internal_attempts=self._max_internal_attempts,
                identical_call_threshold=self._identical_call_threshold,
            )
            outcome = await run_loop(context)
        except RunCancelledError:
            outcome = RunOutcome(kind=OutcomeKind.CANCELLED)
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
        # This is the deliberate terminal boundary that prevents stranded RUNNING runs.
        except Exception as exc:
            logger.exception(
                "run_execution_failed",
                extra={"run_id": str(run.id), "error_class": type(exc).__name__},
            )
            outcome = RunOutcome(
                kind=OutcomeKind.FAILED,
                failure=RunFailure(
                    reason=FailureReason.INTERNAL_ERROR,
                    error_class=type(exc).__name__,
                    message="an unexpected internal error ended the run",
                    step_number=run.step_count or None,
                    occurred_at=self._clock.now(),
                ),
            )
        await finalize(context or finalization, outcome)

    async def _seed_checkpoint(self, run: Run) -> RunCheckpoint:
        events = await self._events.list_after(run.session_id, 0, self._principal)
        messages = [
            event.payload.get("content")
            for event in events
            if event.run_id == run.id and event.event_type == "user.message.created"
        ]
        if not messages or not isinstance(messages[-1], str):
            raise RuntimeError("run has no submitted user message")
        return RunCheckpoint(
            run_id=run.id,
            version=0,
            status=RunStatus.RUNNING,
            conversation=[
                UserMessage(
                    content=[TextPart(text=messages[-1])],
                    principal_id=self._principal.principal_id,
                )
            ],
            created_at=self._clock.now(),
        )

    async def _append(
        self, run: Run, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        await self._events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type=event_type,
                actor_type="runtime",
                payload=payload or {},
            )
        )


def _message_text(message: AssistantMessage | None) -> str | None:
    if message is None:
        return None
    return "\n".join(part.text for part in message.content if isinstance(part, TextPart))


async def finalize(context: RunContext | _FinalizationContext, outcome: RunOutcome) -> None:
    """Perform the terminal checkpoint, transition, and event exactly once."""

    if outcome.kind is OutcomeKind.COMPLETED:
        message = AssistantMessage.model_validate(outcome.final_message)
        await context.runs.transition(
            context.run.id,
            RunStatus.RUNNING,
            RunStatus.COMPLETED,
            final_message=_message_text(message),
        )
        event_type = "run.completed"
        payload: dict[str, Any] = {"final_message": message.model_dump(mode="json")}
    elif outcome.kind is OutcomeKind.CANCELLED:
        await checkpoint(context, "cancelled")
        await context.runs.transition(context.run.id, RunStatus.RUNNING, RunStatus.CANCELLED)
        event_type = "run.cancelled"
        payload = {"reason": getattr(context.token.reason, "value", "requested")}
    elif outcome.kind is OutcomeKind.FAILED:
        if outcome.failure is None:
            raise RuntimeError("failed outcome requires a RunFailure")
        await checkpoint(context, "failed")
        await context.runs.transition(
            context.run.id,
            RunStatus.RUNNING,
            RunStatus.FAILED,
            failure=outcome.failure,
        )
        event_type = "run.failed"
        payload = {"failure": outcome.failure.model_dump(mode="json")}
    else:
        raise RuntimeError(f"Milestone 1 cannot finalize outcome {outcome.kind.value}")
    await context.events.append(
        NewEvent(
            session_id=context.run.session_id,
            run_id=context.run.id,
            event_type=event_type,
            actor_type="runtime",
            payload=payload,
        )
    )
