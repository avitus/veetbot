"""The single Milestone 1 tool invocation pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import ApprovalRequest, ApprovalStatus
from agent_core.domain.errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    ConflictError,
    NotFoundError,
    RunCancelledError,
    ToolValidationError,
    WorkspaceEscape,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import ContentPart, TextPart, ToolCallItem, ToolResultItem
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
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
from agent_core.policy.revalidation import revalidation_denial_reason
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import CancellationToken
from agent_core.ports.execution import WorkspaceFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.policies import PolicyEngine
from agent_core.ports.repositories import ToolInvocationRepository
from agent_core.ports.tools import Tool, ToolRegistry
from agent_core.tools.messages import message_for
from agent_core.tools.validation import validate_and_normalize, validate_output

logger = logging.getLogger(__name__)

_SENSITIVE_ARGUMENT_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|token|authorization|credential)", re.I
)
_CREDENTIAL_SHAPE = re.compile(r"(?:api[_-]?key|secret|password|token|bearer)\s*[:=]\s*\S+", re.I)


def _approval_argument_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_ARGUMENT_KEY.search(key) is not None:
        return "[REDACTED]"
    if isinstance(value, str):
        if _CREDENTIAL_SHAPE.search(value) is not None:
            return "[REDACTED]"
        return value if len(value) <= 512 else f"{value[:512]}…[TRUNCATED]"
    if isinstance(value, dict):
        items = list(value.items())[:50]
        redacted = {
            str(nested_key): _approval_argument_value(nested, key=str(nested_key))
            for nested_key, nested in items
        }
        if len(value) > len(items):
            redacted["[TRUNCATED]"] = f"{len(value) - len(items)} field(s) omitted"
        return redacted
    if isinstance(value, list):
        items = value[:50]
        redacted_items = [_approval_argument_value(item) for item in items]
        if len(value) > len(items):
            redacted_items.append(f"[TRUNCATED: {len(value) - len(items)} item(s) omitted]")
        return redacted_items
    return value


def _approval_argument_view(arguments: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _approval_argument_value(arguments))


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
        *,
        policy: PolicyEngine | None = None,
        workspace_factory: WorkspaceFactory | None = None,
        current_principal: Principal | None = None,
        max_parallel_calls: int = 8,
        approval_expiry_seconds: Mapping[RiskLevel, int] | None = None,
    ) -> None:
        self._registry = registry
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        if policy is None:
            from agent_core.policy.engine import DeterministicPolicyEngine
            from agent_core.policy.loader import DEFAULT_RULESET

            policy = DeterministicPolicyEngine(DEFAULT_RULESET)
        self._policy = policy
        self._workspace_factory = workspace_factory
        self._current_principal = current_principal
        self._max_parallel_calls = max_parallel_calls
        self._approval_expiry_seconds = dict(
            approval_expiry_seconds
            or {
                RiskLevel.LOW: 86_400,
                RiskLevel.MEDIUM: 86_400,
                RiskLevel.HIGH: 14_400,
                RiskLevel.CRITICAL: 3_600,
            }
        )
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
        if self._parallel_ok(tool_calls, run, principal, agent):
            token.raise_if_cancelled()
            parallel_group = self._ids.new_id()
            settled = await asyncio.gather(
                *(
                    self._dispatch_one(
                        run=run,
                        call=call,
                        principal=principal,
                        step=step,
                        agent=agent,
                        token=token,
                        lease=lease,
                        parallel_group=parallel_group,
                    )
                    for call in tool_calls
                ),
                return_exceptions=True,
            )
            for item in settled:
                if isinstance(item, BaseException):
                    raise item
            token.raise_if_cancelled()
            return [cast(ToolResultItem, item) for item in settled]
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
                    parallel_group=None,
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
        parallel_group: UUID | None,
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
        missing = tool.spec.required_scopes - principal.scopes
        if missing:
            return await self._refusal(
                run,
                call,
                "policy.scope.missing",
                ToolOutcomeStatus.DENIED,
                lease,
                message=f"Not performed. Missing required scope(s): {', '.join(sorted(missing))}.",
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
        return await self._execute_once(
            run=run,
            call=call,
            principal=principal,
            agent=agent,
            step=step,
            tool=tool,
            arguments=arguments,
            arguments_hash=arguments_hash,
            key=key,
            token=token,
            lease=lease,
            parallel_group=parallel_group,
        )

    async def _execute_once(
        self,
        *,
        run: Run,
        call: ToolCallItem,
        principal: Principal,
        agent: AgentSpec,
        step: Step,
        tool: Tool,
        arguments: dict[str, Any],
        arguments_hash: str,
        key: str,
        token: CancellationToken,
        lease: WorkerLease | None,
        parallel_group: UUID | None,
        approval_granted: bool = False,
        prepared_candidate: ToolInvocation | None = None,
        prepared_decision: PolicyDecision | None = None,
        lock_acquired: bool = False,
    ) -> ToolResultItem:
        decision = prepared_decision
        candidate = prepared_candidate
        if candidate is None:
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
                side_effect=tool.spec.side_effect,
                risk=tool.spec.risk,
                status=ToolInvocationStatus.PROPOSED,
                raw_arguments=call.raw_arguments,
                normalized_arguments=arguments,
                normalized_arguments_hash=arguments_hash,
                effective_arguments_hash=arguments_hash,
                idempotency_key=key,
                origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
                parallel_group=parallel_group,
                created_at=now,
                updated_at=now,
            )
        if not approval_granted and decision is None:
            action = self._proposed_action(run, step, tool, candidate, arguments, arguments_hash)
            decision = await self._policy.evaluate(action, principal, run)
            effective_hash = arguments_hash
            if decision.decision is PolicyDecisionType.ALLOW_WITH_MODIFICATIONS:
                if decision.modified_arguments is None:
                    raise ConflictError("modified policy decision carried no arguments")
                arguments, _rendered, effective_hash = validate_and_normalize(
                    decision.modified_arguments, tool.spec.input_schema
                )
                key = _idempotency_key(run, step, call, tool, effective_hash)
            candidate = candidate.model_copy(
                update={
                    "effective_arguments_hash": effective_hash,
                    "idempotency_key": key,
                    "policy_decision": decision,
                },
                deep=True,
            )
        if not lock_acquired:
            async with self._key_lock(key):
                return await self._execute_once(
                    run=run,
                    call=call,
                    principal=principal,
                    agent=agent,
                    step=step,
                    tool=tool,
                    arguments=arguments,
                    arguments_hash=arguments_hash,
                    key=key,
                    token=token,
                    lease=lease,
                    parallel_group=parallel_group,
                    approval_granted=approval_granted,
                    prepared_candidate=candidate,
                    prepared_decision=decision,
                    lock_acquired=True,
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
        if recovery is ToolRecoveryAction.RETURN_OUTCOME:
            raise ConflictError("terminal tool invocation has no persisted outcome")
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
            return await self._resume_approval(
                run=run,
                call=call,
                principal=principal,
                agent=agent,
                tool=tool,
                invocation=invocation,
                arguments=arguments,
                arguments_hash=arguments_hash,
                token=token,
                step=step,
                key=key,
                lease=lease,
            )
        if recovery not in {
            ToolRecoveryAction.RESUME_AUTHORIZATION,
            ToolRecoveryAction.REEXECUTE,
            ToolRecoveryAction.REPLAY_IDEMPOTENCY_KEY,
        }:
            raise AssertionError(f"unhandled tool recovery action {recovery.value}")

        if not approval_granted:
            if decision is None:
                raise AssertionError("policy decision was not evaluated")
            invocation = invocation.model_copy(update={"policy_decision": decision}, deep=True)
            if decision.decision is PolicyDecisionType.DENY:
                return await self._deny_invocation(
                    run, call, tool, invocation, decision.reason_code, lease
                )
            if decision.decision is PolicyDecisionType.REQUIRE_APPROVAL:
                approval = await self._request_approval(
                    run, call, principal, agent, tool, invocation, decision, lease
                )
                raise ApprovalRequiredError(approval.id)

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
            workspace=(
                None
                if self._workspace_factory is None
                or tool.spec.side_effect
                not in {SideEffectClass.WORKSPACE_READ, SideEffectClass.WORKSPACE_WRITE}
                else self._workspace_factory.for_run(run.tenant_id, run.id)
            ),
            artifacts=_UnavailableCollaborator(),
            credentials=_UnavailableCollaborator(),
            cancellation=token,
            mark_effect_sent=mark_effect_sent,
        )
        if self._uow_factory.is_open():
            raise RuntimeError("tool execution cannot begin while a unit of work is open")
        try:
            if tool.spec.side_effect is not SideEffectClass.NONE:
                await mark_effect_sent()
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
        except WorkspaceEscape:
            result = ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.INVALID_ARGUMENTS,
                    reason_code="tool.arguments_invalid",
                    detail="workspace path failed containment validation",
                    retryable=False,
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

    def _proposed_action(
        self,
        run: Run,
        step: Step,
        tool: Tool,
        invocation: ToolInvocation,
        arguments: dict[str, Any],
        arguments_hash: str,
    ) -> ProposedAction:
        return ProposedAction(
            kind=ActionKind.TOOL_CALL,
            action_id=invocation.id,
            tenant_id=run.tenant_id,
            session_id=run.session_id,
            run_id=run.id,
            step_number=step.step_number,
            name=tool.spec.name,
            version=tool.spec.version,
            summary=f"Run {tool.spec.name} with validated arguments.",
            side_effect=tool.spec.side_effect,
            risk=tool.spec.risk,
            idempotency=tool.spec.idempotency,
            required_scopes=set(tool.spec.required_scopes),
            arguments=arguments,
            normalized_arguments_hash=arguments_hash,
            argument_trust=dict.fromkeys(arguments, TrustLevel.EXTERNAL_UNTRUSTED),
            origin_trust=invocation.origin_trust,
            target=ExecutionTarget(
                kind=tool.spec.target_kind,
                isolated=tool.spec.target_kind == "sandbox",
                network_enabled=False,
                server_id=tool.spec.server_id,
            ),
            evaluated_at=self._clock.now(),
        )

    async def _request_approval(
        self,
        run: Run,
        call: ToolCallItem,
        principal: Principal,
        agent: AgentSpec,
        tool: Tool,
        invocation: ToolInvocation,
        decision: PolicyDecision,
        lease: WorkerLease | None,
    ) -> ApprovalRequest:
        if invocation.normalized_arguments is None or invocation.normalized_arguments_hash is None:
            raise ConflictError("approval action has no normalized arguments")
        now = self._clock.now()
        approval = ApprovalRequest(
            id=self._ids.new_id(),
            tenant_id=run.tenant_id,
            principal_id=principal.principal_id,
            session_id=run.session_id,
            run_id=run.id,
            action_kind=ActionKind.TOOL_CALL,
            action_id=invocation.id,
            tool_invocation_id=invocation.id,
            status=ApprovalStatus.PENDING,
            action_summary=f"Run {tool.spec.name} with validated arguments.",
            tool_name=tool.spec.name,
            arguments=_approval_argument_view(dict(invocation.normalized_arguments)),
            normalized_arguments_hash=invocation.normalized_arguments_hash,
            required_scopes=set(tool.spec.required_scopes),
            agent_version=agent.version,
            risk=tool.spec.risk,
            policy_reason=decision.reason_code,
            policy_decision=decision,
            policy_version=decision.policy_version,
            expires_at=now + timedelta(seconds=self._approval_expiry_seconds[tool.spec.risk]),
            created_at=now,
        )
        waiting = invocation.model_copy(
            update={
                "status": ToolInvocationStatus.WAITING_FOR_APPROVAL,
                "policy_decision": decision,
                "suspended_kind": "approval",
                "suspended_ref": str(approval.id),
                "updated_at": now,
            },
            deep=True,
        )
        async with self._uow_factory() as uow:
            created = await uow.approvals.create(approval)
            try:
                await uow.invocations.transition(
                    invocation.id, invocation.status, waiting, lease=lease
                )
            except BaseException:
                with suppress(BaseException):
                    await uow.approvals.discard_pending(created.id)
                raise
        return created

    async def _resume_approval(
        self,
        *,
        run: Run,
        call: ToolCallItem,
        principal: Principal,
        agent: AgentSpec,
        tool: Tool,
        invocation: ToolInvocation,
        arguments: dict[str, Any],
        arguments_hash: str,
        token: CancellationToken,
        step: Step,
        key: str,
        lease: WorkerLease | None,
    ) -> ToolResultItem:
        async with self._uow_factory() as uow:
            approval = await uow.approvals.get_by_action(invocation.id)
        if approval is None:
            raise ConflictError("waiting invocation has no approval")
        if approval.status is ApprovalStatus.PENDING:
            raise ApprovalRequiredError(approval.id)
        if approval.status is not ApprovalStatus.APPROVED:
            reason = {
                ApprovalStatus.DENIED: "approval.denied",
                ApprovalStatus.EXPIRED: "approval.expired",
                ApprovalStatus.CANCELLED: "approval.cancelled",
            }[approval.status]
            return await self._deny_invocation(run, call, tool, invocation, reason, lease)
        current = principal
        if self._current_principal is not None:
            if (
                self._current_principal.tenant_id != principal.tenant_id
                or self._current_principal.principal_id != principal.principal_id
            ):
                raise ConflictError("approval principal does not match the run principal")
            current = principal.model_copy(
                update={
                    "scopes": principal.scopes & self._current_principal.scopes,
                },
                deep=True,
            )
        action = self._proposed_action(run, step, tool, invocation, arguments, arguments_hash)
        revalidated = await self._policy.evaluate(action, current, run)
        async with self._uow_factory() as uow:
            approval = await uow.approvals.record_revalidation(
                invocation.id, revalidated.policy_version
            )
        denial_reason = revalidation_denial_reason(
            approval,
            arguments_hash=arguments_hash,
            principal_scopes=current.scopes,
            agent_version=agent.version,
            policy_version=revalidated.policy_version,
            policy_decision=revalidated.decision,
        )
        if denial_reason is not None:
            return await self._deny_invocation(run, call, tool, invocation, denial_reason, lease)
        approved = invocation.model_copy(
            update={
                "status": ToolInvocationStatus.AUTHORIZED,
                "policy_decision": revalidated,
                "suspended_kind": None,
                "suspended_ref": None,
                "updated_at": self._clock.now(),
            },
            deep=True,
        )
        async with self._uow_factory() as uow:
            invocation = await uow.invocations.transition(
                invocation.id,
                ToolInvocationStatus.WAITING_FOR_APPROVAL,
                approved,
                lease=lease,
            )
            await self._event_in(
                uow,
                run,
                "tool.call.authorized",
                {"name": call.name, "call_id": call.call_id},
                lease,
            )
        return await self._execute_once(
            run=run,
            call=call,
            principal=current,
            agent=agent,
            step=step,
            tool=tool,
            arguments=arguments,
            arguments_hash=arguments_hash,
            key=key,
            token=token,
            lease=lease,
            approval_granted=True,
            parallel_group=invocation.parallel_group,
            lock_acquired=True,
        )

    def _parallel_ok(
        self,
        calls: list[ToolCallItem],
        run: Run,
        principal: Principal,
        agent: AgentSpec,
    ) -> bool:
        if (
            len(calls) <= 1
            or len(calls) > self._max_parallel_calls
            or run.tool_call_count + len(calls) > run.limits.max_tool_calls
        ):
            return False
        read_only_effects = {
            SideEffectClass.NONE,
            SideEffectClass.WORKSPACE_READ,
            SideEffectClass.NETWORK_READ,
        }
        for call in calls:
            try:
                tool = self._registry.get(call.name)
            except NotFoundError:
                return False
            if (
                call.name not in agent.enabled_tools
                or not tool.spec.required_scopes.issubset(principal.scopes)
                or tool.spec.side_effect not in read_only_effects
                or not tool.spec.allow_parallel
                or tool.spec.idempotency is not IdempotencyClass.READ_ONLY
            ):
                return False
        return True

    async def _deny_invocation(
        self,
        run: Run,
        call: ToolCallItem,
        tool: Tool,
        invocation: ToolInvocation,
        reason_code: str,
        lease: WorkerLease | None,
    ) -> ToolResultItem:
        denied = ToolOutcome(
            status=ToolOutcomeStatus.DENIED,
            action=call.name,
            reason_code=reason_code,
            message=message_for(reason_code),
            retryable=False,
            remediation="none",
        )
        result_item = _outcome_item(call.call_id, denied, tool.spec.output_trust)
        final = invocation.model_copy(
            update={
                "status": ToolInvocationStatus.DENIED,
                "outcome": denied,
                "result_item": result_item,
                "suspended_kind": None,
                "suspended_ref": None,
                "updated_at": self._clock.now(),
            },
            deep=True,
        )
        async with self._uow_factory() as uow:
            await uow.invocations.transition(invocation.id, invocation.status, final, lease=lease)
            if reason_code.startswith("policy.revalidation."):
                await self._event_in(
                    uow,
                    run,
                    "approval.invalidated",
                    {
                        "approval_id": invocation.suspended_ref,
                        "action_id": str(invocation.id),
                        "reason_code": reason_code,
                    },
                    lease,
                )
            await self._event_in(
                uow,
                run,
                "tool.call.denied",
                {
                    "name": call.name,
                    "call_id": call.call_id,
                    "reason_code": reason_code,
                },
                lease,
            )
        return result_item

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
                "structured_result": result.structured,
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
        message: str | None = None,
    ) -> ToolResultItem:
        outcome = ToolOutcome(
            status=status,
            action=call.name,
            reason_code=reason_code,
            message=message or message_for(reason_code),
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
