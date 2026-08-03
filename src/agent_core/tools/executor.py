"""The single Milestone 1 tool invocation pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.errors import (
    BudgetExceededError,
    ConflictError,
    NotFoundError,
    RunCancelledError,
    ToolValidationError,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import ContentPart, TextPart, ToolCallItem, ToolResultItem
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.policies import (
    ExecutionTarget,
    IdempotencyClass,
    SideEffectClass,
    TrustLevel,
)
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
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.repositories import ToolInvocationRepository
from agent_core.ports.tools import Tool, ToolRegistry
from agent_core.tools.messages import message_for
from agent_core.tools.validation import validate_and_normalize, validate_output

logger = logging.getLogger(__name__)


class _UnavailableCollaborator:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"collaborator {name!r} is unavailable in the Milestone 1 tier")


class ToolRecoveryAction(StrEnum):
    RETURN_OUTCOME = "return_outcome"
    RESUME_AUTHORIZATION = "resume_authorization"
    RESUME_APPROVAL = "resume_approval"
    REEXECUTE = "reexecute"
    REPLAY_IDEMPOTENCY_KEY = "replay_idempotency_key"
    MARK_UNCERTAIN = "mark_uncertain"


@dataclass(slots=True)
class _KeyLockEntry:
    lock: asyncio.Lock
    users: int = 0


def tool_recovery_action(invocation: ToolInvocation) -> ToolRecoveryAction:
    """Return the total recovery-table decision for one persisted invocation."""

    if invocation.outcome is not None or invocation.status in {
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.FAILED,
        ToolInvocationStatus.DENIED,
        ToolInvocationStatus.UNCERTAIN,
    }:
        return ToolRecoveryAction.RETURN_OUTCOME
    if invocation.status in {
        ToolInvocationStatus.PROPOSED,
        ToolInvocationStatus.AUTHORIZED,
    }:
        return ToolRecoveryAction.RESUME_AUTHORIZATION
    if invocation.status is ToolInvocationStatus.WAITING_FOR_APPROVAL:
        return ToolRecoveryAction.RESUME_APPROVAL
    if invocation.status is not ToolInvocationStatus.RUNNING:
        raise ConflictError(f"unknown tool recovery status {invocation.status.value}")
    if invocation.idempotency_class in {
        IdempotencyClass.READ_ONLY,
        IdempotencyClass.IDEMPOTENT,
    }:
        return ToolRecoveryAction.REEXECUTE
    if invocation.effect_sent_at is None:
        return ToolRecoveryAction.REEXECUTE
    if invocation.idempotency_class is IdempotencyClass.CONDITIONALLY_IDEMPOTENT:
        return ToolRecoveryAction.REPLAY_IDEMPOTENCY_KEY
    return ToolRecoveryAction.MARK_UNCERTAIN


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
    lease: WorkerLease | None = None,
) -> ToolInvocation:
    """The sole PROPOSED-to-AUTHORIZED transition in the tool system."""

    authorized = invocation.model_copy(
        update={"status": ToolInvocationStatus.AUTHORIZED, "updated_at": clock.now()}, deep=True
    )
    return await repository.transition(
        invocation.id,
        ToolInvocationStatus.PROPOSED,
        authorized,
        lease=lease,
    )


class ToolPipeline:
    """Resolve, scope, validate, authorize, execute, and persist every tool call."""

    def __init__(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._registry = registry
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._key_locks: dict[str, _KeyLockEntry] = {}
        self._key_locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def _key_lock(self, key: str) -> AsyncIterator[None]:
        async with self._key_locks_guard:
            entry = self._key_locks.setdefault(key, _KeyLockEntry(lock=asyncio.Lock()))
            entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._key_locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._key_locks.get(key) is entry:
                    del self._key_locks[key]

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
        lease: WorkerLease | None = None,
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
                    lease=lease,
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
        lease: WorkerLease | None,
    ) -> ToolResultItem:
        try:
            tool = self._registry.get(call.name)
        except NotFoundError:
            return await self._refusal(
                run,
                call,
                "policy.matrix.unknown_tool",
                ToolOutcomeStatus.DENIED,
                lease,
            )
        if call.name not in agent.enabled_tools:
            return await self._refusal(
                run,
                call,
                "policy.matrix.unknown_tool",
                ToolOutcomeStatus.DENIED,
                lease,
            )
        if not tool.spec.required_scopes.issubset(principal.scopes):
            return await self._refusal(
                run, call, "policy.scope.missing", ToolOutcomeStatus.DENIED, lease
            )
        if call.parse_error is not None:
            return await self._refusal(
                run, call, "tool.arguments_invalid", ToolOutcomeStatus.FAILED, lease
            )
        try:
            arguments, _rendered, arguments_hash = validate_and_normalize(
                call.arguments, tool.spec.input_schema
            )
        except ToolValidationError:
            return await self._refusal(
                run, call, "tool.arguments_invalid", ToolOutcomeStatus.FAILED, lease
            )

        key = _idempotency_key(run, step, call, tool, arguments_hash)
        async with self._key_lock(key):
            return await self._execute_once(
                run=run,
                call=call,
                principal=principal,
                step=step,
                tool=tool,
                arguments=arguments,
                arguments_hash=arguments_hash,
                key=key,
                token=token,
                lease=lease,
            )

    async def _execute_once(
        self,
        *,
        run: Run,
        call: ToolCallItem,
        principal: Principal,
        step: Step,
        tool: Tool,
        arguments: dict[str, Any],
        arguments_hash: str,
        key: str,
        token: CancellationToken,
        lease: WorkerLease | None,
    ) -> ToolResultItem:
        now = self._clock.now()
        candidate = ToolInvocation(
            id=self._ids.new_id(),
            run_id=run.id,
            session_id=run.session_id,
            step_number=step.step_number,
            call_id=call.call_id,
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            tool_source=tool.spec.source,
            server_id=tool.spec.server_id,
            idempotency_class=tool.spec.idempotency,
            status=ToolInvocationStatus.PROPOSED,
            raw_arguments=call.raw_arguments,
            normalized_arguments=arguments,
            normalized_arguments_hash=arguments_hash,
            idempotency_key=key,
            origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            created_at=now,
            updated_at=now,
        )
        async with self._uow_factory() as uow:
            existing = await uow.invocations.find_by_idempotency_key(run.id, key)
            if existing is not None:
                invocation = existing
            else:
                invocation = await uow.invocations.create(candidate, lease=lease)
                if invocation.id == candidate.id:
                    await self._event_in(
                        uow,
                        run,
                        "tool.call.proposed",
                        {"name": call.name, "call_id": call.call_id},
                        lease,
                    )
        if invocation.outcome is not None:
            return invocation.result_item or _outcome_item(
                call.call_id, invocation.outcome, tool.spec.output_trust
            )
        recovery = tool_recovery_action(invocation)
        if recovery is ToolRecoveryAction.MARK_UNCERTAIN:
            outcome = ToolOutcome(
                status=ToolOutcomeStatus.UNCERTAIN,
                action=call.name,
                reason_code="tool.outcome_unknown",
                message=message_for("tool.outcome_unknown"),
                retryable=False,
                remediation="none",
            )
            result_item = _outcome_item(call.call_id, outcome, tool.spec.output_trust)
            uncertain = invocation.model_copy(
                update={
                    "status": ToolInvocationStatus.UNCERTAIN,
                    "outcome": outcome,
                    "result_item": result_item,
                    "updated_at": self._clock.now(),
                },
                deep=True,
            )
            async with self._uow_factory() as uow:
                await uow.invocations.transition(
                    invocation.id,
                    ToolInvocationStatus.RUNNING,
                    uncertain,
                    lease=lease,
                )
                await self._event_in(
                    uow,
                    run,
                    "tool.call.uncertain",
                    {
                        "name": call.name,
                        "call_id": call.call_id,
                        "reason_code": outcome.reason_code,
                        "result_item": result_item.model_dump(mode="json"),
                    },
                    lease,
                )
            return result_item
        if recovery is ToolRecoveryAction.RESUME_APPROVAL:
            raise ConflictError("approval recovery is not authorized before Milestone 4")

        if tool.spec.side_effect is not SideEffectClass.NONE:
            denied = ToolOutcome(
                status=ToolOutcomeStatus.DENIED,
                action=call.name,
                reason_code="policy.milestone1.non_pure",
                message=message_for("policy.milestone1.non_pure"),
                retryable=False,
                remediation="none",
            )
            result_item = _outcome_item(call.call_id, denied, tool.spec.output_trust)
            final = invocation.model_copy(
                update={
                    "status": ToolInvocationStatus.DENIED,
                    "outcome": denied,
                    "result_item": result_item,
                    "updated_at": self._clock.now(),
                },
                deep=True,
            )
            async with self._uow_factory() as uow:
                await uow.invocations.transition(
                    invocation.id,
                    invocation.status,
                    final,
                    lease=lease,
                )
                await self._event_in(
                    uow,
                    run,
                    "tool.call.denied",
                    {
                        "name": call.name,
                        "call_id": call.call_id,
                        "reason_code": denied.reason_code,
                    },
                    lease,
                )
            return result_item

        if invocation.status is ToolInvocationStatus.PROPOSED:
            async with self._uow_factory() as uow:
                invocation = await authorize_tool_invocation(
                    uow.invocations, invocation, self._clock, lease
                )
                await self._event_in(
                    uow,
                    run,
                    "tool.call.authorized",
                    {"name": call.name, "call_id": call.call_id},
                    lease,
                )
        if invocation.status is ToolInvocationStatus.AUTHORIZED:
            running = invocation.model_copy(
                update={"status": ToolInvocationStatus.RUNNING, "updated_at": self._clock.now()},
                deep=True,
            )
            async with self._uow_factory() as uow:
                invocation = await uow.invocations.transition(
                    invocation.id,
                    ToolInvocationStatus.AUTHORIZED,
                    running,
                    lease=lease,
                )
                await self._event_in(
                    uow,
                    run,
                    "tool.call.started",
                    {"name": call.name, "call_id": call.call_id},
                    lease,
                )
        if invocation.status is not ToolInvocationStatus.RUNNING:
            raise ConflictError(f"cannot recover tool invocation in {invocation.status.value}")

        effect_guard = asyncio.Lock()

        async def mark_effect_sent() -> None:
            nonlocal invocation
            async with effect_guard:
                if invocation.effect_sent_at is not None:
                    return
                marked_at = self._clock.now()
                pending = invocation.model_copy(
                    update={"effect_sent_at": marked_at, "updated_at": marked_at},
                    deep=True,
                )
                async with self._uow_factory() as uow:
                    invocation = await uow.invocations.transition(
                        pending.id,
                        ToolInvocationStatus.RUNNING,
                        pending,
                        lease=lease,
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
        if self._uow_factory.is_open():
            raise RuntimeError("tool execution cannot begin while a unit of work is open")
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
        return await self._finish(run, call, tool, invocation, result, lease)

    async def _finish(
        self,
        run: Run,
        call: ToolCallItem,
        tool: Tool,
        invocation: ToolInvocation,
        result: ToolResult,
        lease: WorkerLease | None,
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
        if result.ok:
            trust = result.output_trust or tool.spec.output_trust
            if tool.spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED:
                trust = TrustLevel.EXTERNAL_UNTRUSTED
            result_item = ToolResultItem(call_id=call.call_id, content=result.content, trust=trust)
        else:
            result_item = _outcome_item(
                call.call_id,
                outcome,
                tool.spec.output_trust,
                result.failure.external_text if result.failure is not None else None,
            )
        finished = invocation.model_copy(
            update={
                "status": status,
                "outcome": outcome,
                "result_item": result_item,
                "updated_at": self._clock.now(),
            },
            deep=True,
        )
        async with self._uow_factory() as uow:
            await uow.invocations.transition(
                invocation.id,
                ToolInvocationStatus.RUNNING,
                finished,
                lease=lease,
            )
            await self._event_in(
                uow,
                run,
                event_type,
                {
                    "name": call.name,
                    "call_id": call.call_id,
                    "reason_code": outcome.reason_code,
                    "result_item": result_item.model_dump(mode="json"),
                },
                lease,
            )
        return result_item

    async def _refusal(
        self,
        run: Run,
        call: ToolCallItem,
        reason_code: str,
        status: ToolOutcomeStatus,
        lease: WorkerLease | None,
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
        result_item = _outcome_item(call.call_id, outcome, TrustLevel.INTERNAL_TOOL)
        async with self._uow_factory() as uow:
            await self._event_in(
                uow,
                run,
                event_type,
                {
                    "name": call.name,
                    "call_id": call.call_id,
                    "reason_code": reason_code,
                    "result_item": result_item.model_dump(mode="json"),
                },
                lease,
            )
        return result_item

    @staticmethod
    async def _event_in(
        uow: RepositoryUnitOfWork,
        run: Run,
        event_type: str,
        payload: dict[str, Any],
        lease: WorkerLease | None,
    ) -> None:
        await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type=event_type,
                actor_type="runtime",
                payload=payload,
            ),
            lease=lease,
        )
