"""The single Milestone 1 tool invocation pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta
from typing import Any

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.errors import (
    BudgetExceededError,
    NotFoundError,
    RunCancelledError,
    ToolValidationError,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import ContentPart, TextPart, ToolCallItem, ToolResultItem
from agent_core.domain.policies import ExecutionTarget, SideEffectClass, TrustLevel
from agent_core.domain.runs import Run, RunCheckpoint, Step
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolInvocation,
    ToolInvocationStatus,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolResult,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import CancellationToken
from agent_core.ports.events import EventRepository
from agent_core.ports.repositories import ToolInvocationRepository
from agent_core.ports.tools import Tool, ToolRegistry
from agent_core.tools.messages import message_for
from agent_core.tools.validation import validate_and_normalize, validate_output

logger = logging.getLogger(__name__)


class _UnavailableCollaborator:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"collaborator {name!r} is unavailable in the Milestone 1 tier")


def _idempotency_key(
    run: Run,
    step: Step,
    call: ToolCallItem,
    tool: Tool,
    arguments_hash: str,
) -> str:
    material = ":".join(
        (
            "v1",
            str(run.id),
            str(step.step_number),
            call.call_id,
            tool.spec.name,
            tool.spec.version,
            arguments_hash,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _outcome_item(
    call_id: str, outcome: ToolOutcome, trust: TrustLevel, external_text: str | None = None
) -> ToolResultItem:
    content: list[ContentPart] = [TextPart(text=outcome.model_dump_json())]
    if external_text is not None:
        content.append(TextPart(text=external_text))
        trust = TrustLevel.EXTERNAL_UNTRUSTED
    return ToolResultItem(
        call_id=call_id,
        content=content,
        is_error=outcome.status is not ToolOutcomeStatus.SUCCEEDED,
        trust=trust,
    )


async def authorize_tool_invocation(
    repository: ToolInvocationRepository,
    invocation: ToolInvocation,
    clock: Clock,
) -> ToolInvocation:
    """The sole PROPOSED-to-AUTHORIZED transition in the tool system."""

    authorized = invocation.model_copy(
        update={"status": ToolInvocationStatus.AUTHORIZED, "updated_at": clock.now()}, deep=True
    )
    return await repository.transition(invocation.id, ToolInvocationStatus.PROPOSED, authorized)


class ToolPipeline:
    """Resolve, scope, validate, authorize, execute, and persist every tool call."""

    def __init__(
        self,
        registry: ToolRegistry,
        invocations: ToolInvocationRepository,
        events: EventRepository,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._registry = registry
        self._invocations = invocations
        self._events = events
        self._clock = clock
        self._ids = ids

    async def dispatch(
        self,
        *,
        run: Run,
        checkpoint: RunCheckpoint,
        tool_calls: list[ToolCallItem],
        principal: Principal,
        step: Step,
        agent: AgentSpec,
        token: CancellationToken,
    ) -> list[ToolResultItem]:
        del checkpoint
        results: list[ToolResultItem] = []
        for call in tool_calls:
            token.raise_if_cancelled()
            results.append(
                await self._dispatch_one(
                    run=run,
                    call=call,
                    principal=principal,
                    step=step,
                    agent=agent,
                    token=token,
                )
            )
            token.raise_if_cancelled()
        return results

    async def _dispatch_one(
        self,
        *,
        run: Run,
        call: ToolCallItem,
        principal: Principal,
        step: Step,
        agent: AgentSpec,
        token: CancellationToken,
    ) -> ToolResultItem:
        try:
            tool = self._registry.get(call.name)
        except NotFoundError:
            return await self._refusal(
                run,
                call,
                "policy.matrix.unknown_tool",
                ToolOutcomeStatus.DENIED,
            )
        if call.name not in agent.enabled_tools:
            return await self._refusal(
                run,
                call,
                "policy.matrix.unknown_tool",
                ToolOutcomeStatus.DENIED,
            )
        if not tool.spec.required_scopes.issubset(principal.scopes):
            return await self._refusal(run, call, "policy.scope.missing", ToolOutcomeStatus.DENIED)
        if call.parse_error is not None:
            return await self._refusal(
                run, call, "tool.arguments_invalid", ToolOutcomeStatus.FAILED
            )
        try:
            arguments, _rendered, arguments_hash = validate_and_normalize(
                call.arguments, tool.spec.input_schema
            )
        except ToolValidationError:
            return await self._refusal(
                run, call, "tool.arguments_invalid", ToolOutcomeStatus.FAILED
            )

        key = _idempotency_key(run, step, call, tool, arguments_hash)
        existing = await self._invocations.find_by_idempotency_key(run.id, key)
        if existing is not None and existing.outcome is not None:
            return _outcome_item(call.call_id, existing.outcome, tool.spec.output_trust)

        now = self._clock.now()
        invocation = ToolInvocation(
            id=self._ids.new_id(),
            run_id=run.id,
            session_id=run.session_id,
            step_number=step.step_number,
            call_id=call.call_id,
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            status=ToolInvocationStatus.PROPOSED,
            raw_arguments=call.raw_arguments,
            normalized_arguments=arguments,
            normalized_arguments_hash=arguments_hash,
            idempotency_key=key,
            created_at=now,
            updated_at=now,
        )
        invocation = await self._invocations.create(invocation)
        await self._event(run, "tool.call.proposed", {"name": call.name, "call_id": call.call_id})

        if tool.spec.side_effect is not SideEffectClass.NONE:
            denied = ToolOutcome(
                status=ToolOutcomeStatus.DENIED,
                action=call.name,
                reason_code="policy.milestone1.non_pure",
                message=message_for("policy.milestone1.non_pure"),
                retryable=False,
                remediation="none",
            )
            final = invocation.model_copy(
                update={
                    "status": ToolInvocationStatus.DENIED,
                    "outcome": denied,
                    "updated_at": self._clock.now(),
                },
                deep=True,
            )
            await self._invocations.transition(invocation.id, ToolInvocationStatus.PROPOSED, final)
            await self._event(
                run,
                "tool.call.denied",
                {"name": call.name, "call_id": call.call_id, "reason_code": denied.reason_code},
            )
            return _outcome_item(call.call_id, denied, tool.spec.output_trust)

        invocation = await authorize_tool_invocation(self._invocations, invocation, self._clock)
        await self._event(run, "tool.call.authorized", {"name": call.name, "call_id": call.call_id})
        running = invocation.model_copy(
            update={"status": ToolInvocationStatus.RUNNING, "updated_at": self._clock.now()},
            deep=True,
        )
        invocation = await self._invocations.transition(
            invocation.id, ToolInvocationStatus.AUTHORIZED, running
        )
        await self._event(run, "tool.call.started", {"name": call.name, "call_id": call.call_id})

        async def mark_effect_sent() -> None:
            nonlocal invocation
            invocation = invocation.model_copy(
                update={"effect_sent_at": self._clock.now(), "updated_at": self._clock.now()},
                deep=True,
            )
            invocation = await self._invocations.transition(
                invocation.id, ToolInvocationStatus.RUNNING, invocation
            )

        deadline = self._clock.now() + timedelta(seconds=tool.spec.timeout_seconds)
        if run.deadline_at is not None:
            deadline = min(deadline, run.deadline_at)
        execution_context = ToolExecutionContext(
            invocation_id=invocation.id,
            call_id=call.call_id,
            run_id=run.id,
            session_id=run.session_id,
            tenant_id=run.tenant_id,
            principal=principal,
            step_number=step.step_number,
            attempt_number=1,
            idempotency_key=key,
            deadline_at=deadline,
            timeout_seconds=tool.spec.timeout_seconds,
            maximum_output_bytes=tool.spec.maximum_output_bytes,
            target=ExecutionTarget(
                kind=tool.spec.target_kind,
                isolated=tool.spec.target_kind == "sandbox",
                network_enabled=False,
            ),
            workspace=None,
            artifacts=_UnavailableCollaborator(),
            credentials=_UnavailableCollaborator(),
            cancellation=token,
            mark_effect_sent=mark_effect_sent,
        )
        try:
            async with asyncio.timeout(tool.spec.timeout_seconds):
                result = await tool.execute(arguments, execution_context)
            if result.ok:
                validate_output(result.structured, tool.spec.output_schema)
            if (
                result.ok
                and sum(
                    len(part.text.encode("utf-8"))
                    for part in result.content
                    if isinstance(part, TextPart)
                )
                > tool.spec.maximum_output_bytes
            ):
                result = ToolResult(
                    ok=False,
                    content=[],
                    failure=ToolFailure(
                        kind=ToolFailureKind.OUTPUT_TOO_LARGE,
                        reason_code="tool.output_invalid",
                        detail="tool output exceeded its declared byte limit",
                        retryable=False,
                    ),
                )
        except TimeoutError:
            result = ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.TIMEOUT,
                    reason_code="tool.timeout",
                    detail="tool timeout elapsed",
                    retryable=True,
                ),
            )
        except ToolValidationError:
            result = ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.OUTPUT_INVALID,
                    reason_code="tool.output_invalid",
                    detail="tool output schema validation failed",
                    retryable=False,
                ),
            )
        except (RunCancelledError, BudgetExceededError, NotFoundError):
            raise
        # Tool implementation failures are deliberately normalized at this boundary.
        except Exception as exc:
            logger.exception(
                "tool_execution_failed",
                extra={
                    "tool_name": tool.spec.name,
                    "error_class": type(exc).__name__,
                },
            )
            result = ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.INTERNAL,
                    reason_code="tool.internal_error",
                    detail="tool raised an unexpected exception",
                    retryable=False,
                ),
            )
        return await self._finish(run, call, tool, invocation, result)

    async def _finish(
        self,
        run: Run,
        call: ToolCallItem,
        tool: Tool,
        invocation: ToolInvocation,
        result: ToolResult,
    ) -> ToolResultItem:
        if result.ok:
            outcome = ToolOutcome(
                status=ToolOutcomeStatus.SUCCEEDED,
                action=call.name,
                reason_code="tool.succeeded",
                message=message_for("tool.succeeded"),
                retryable=False,
                remediation="none",
            )
            status = ToolInvocationStatus.SUCCEEDED
            event_type = "tool.call.completed"
        else:
            if result.failure is None:
                raise RuntimeError("ToolResult contract violation: failed result has no failure")
            outcome = ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                action=call.name,
                reason_code=result.failure.reason_code,
                message=message_for(result.failure.reason_code),
                retryable=result.failure.retryable,
                remediation="modify_arguments" if result.failure.retryable else "none",
            )
            status = ToolInvocationStatus.FAILED
            event_type = "tool.call.failed"
        finished = invocation.model_copy(
            update={"status": status, "outcome": outcome, "updated_at": self._clock.now()},
            deep=True,
        )
        await self._invocations.transition(invocation.id, ToolInvocationStatus.RUNNING, finished)
        await self._event(
            run,
            event_type,
            {
                "name": call.name,
                "call_id": call.call_id,
                "reason_code": outcome.reason_code,
            },
        )
        if result.ok:
            trust = result.output_trust or tool.spec.output_trust
            if tool.spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED:
                trust = TrustLevel.EXTERNAL_UNTRUSTED
            return ToolResultItem(call_id=call.call_id, content=result.content, trust=trust)
        return _outcome_item(
            call.call_id,
            outcome,
            tool.spec.output_trust,
            result.failure.external_text if result.failure is not None else None,
        )

    async def _refusal(
        self,
        run: Run,
        call: ToolCallItem,
        reason_code: str,
        status: ToolOutcomeStatus,
    ) -> ToolResultItem:
        outcome = ToolOutcome(
            status=status,
            action=call.name,
            reason_code=reason_code,
            message=message_for(reason_code),
            retryable=False,
            remediation="modify_arguments" if status is ToolOutcomeStatus.FAILED else "none",
        )
        event_type = (
            "tool.call.failed" if status is ToolOutcomeStatus.FAILED else "tool.call.denied"
        )
        await self._event(
            run,
            event_type,
            {"name": call.name, "call_id": call.call_id, "reason_code": reason_code},
        )
        return _outcome_item(call.call_id, outcome, TrustLevel.INTERNAL_TOOL)

    async def _event(self, run: Run, event_type: str, payload: dict[str, Any]) -> None:
        await self._events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type=event_type,
                actor_type="runtime",
                payload=payload,
            )
        )
