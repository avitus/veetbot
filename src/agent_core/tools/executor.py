"""The single Milestone 1 tool invocation pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import ApprovalRequest, ApprovalStatus
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.delegations import Delegation, DelegationRequest
from agent_core.domain.errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    ChildRunRequiredError,
    ConflictError,
    DelegationValidationError,
    NotFoundError,
    RunCancelledError,
    ToolTrustRejectedError,
    ToolValidationError,
    UserInputRequiredError,
    WorkspaceEscape,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    ContentPart,
    FileReferencePart,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
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
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunCheckpoint, Step
from agent_core.domain.skills import LoadedSkillBody
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolInvocation,
    ToolInvocationStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolResult,
    ToolSource,
    ToolSpec,
)
from agent_core.policy.revalidation import revalidation_denial_reason
from agent_core.ports.artifacts import ArtifactWriterProvider
from agent_core.ports.credentials import CredentialResolver, UnavailableCredentialResolver
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import CancellationToken
from agent_core.ports.execution import WorkspaceFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.policies import PolicyEngine, StandingAuthorizer
from agent_core.ports.repositories import ToolInvocationRepository
from agent_core.ports.tools import Tool, ToolRegistry
from agent_core.tools.context_update import WORKING_STATE_TOOL_NAME
from agent_core.tools.delegate_run import DELEGATE_RUN_TOOL_NAME
from agent_core.tools.messages import message_for
from agent_core.tools.skill_load import SKILL_LOAD_TOOL_NAME
from agent_core.tools.skill_manage import SKILL_MANAGE_TOOL_NAME
from agent_core.tools.validation import validate_and_normalize, validate_output

logger = logging.getLogger(__name__)

PIPELINE_STEP_SEQUENCE = tuple(range(1, 15))


class DelegationStarter(Protocol):
    """Materialize one delegation for a suspending delegate.run invocation."""

    async def materialize(
        self,
        *,
        request: DelegationRequest,
        run: Run,
        agent: AgentSpec,
        principal: Principal,
        invocation: ToolInvocation,
        pinned_tools: Mapping[str, ToolSpec],
        lease: WorkerLease | None = None,
    ) -> Delegation: ...


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


@dataclass(frozen=True, slots=True)
class PipelineTrace:
    run_id: UUID
    call_id: str
    tool_name: str
    tool_source: ToolSource
    steps: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _PreparedContextUpdate:
    loaded_skills: tuple[LoadedSkillBody, ...] | None = None
    working_state: dict[str, Any] | None = None


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


def _effective_output_trust(result: ToolResult, tool: Tool) -> TrustLevel:
    trust = result.output_trust or tool.spec.output_trust
    if tool.spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED:
        return TrustLevel.EXTERNAL_UNTRUSTED
    return trust


def _writer_origin(tool: Tool) -> ArtifactOrigin:
    # artifact.export is intentionally an in-process capability over a sandbox
    # workspace; its persisted object is nevertheless a sandbox export.
    if tool.spec.name == "artifact.export":
        return ArtifactOrigin.SANDBOX_EXPORT
    return ArtifactOrigin.TOOL_OUTPUT


def _action_kind(tool: Tool) -> ActionKind:
    if tool.spec.name == "memory.remember":
        return ActionKind.MEMORY_WRITE
    if tool.spec.name == SKILL_MANAGE_TOOL_NAME:
        return ActionKind.SKILL_AUTHORING
    return ActionKind.TOOL_CALL


def _turn_origin_trust(checkpoint: RunCheckpoint, run_kind: str = "interactive") -> TrustLevel:
    """Conservatively taint writes when the active turn consumed external content."""

    origin_trust = checkpoint.context_origin_trust
    active_turn: list[object] = []
    for item in reversed(checkpoint.conversation):
        active_turn.append(item)
        if isinstance(item, UserMessage):
            # The turn is opened by the newest user message, whatever its
            # trust. A device-ingested message is not laundered by an owner
            # message earlier in the same standing session, and the checkpoint
            # stamp cannot carry the fact either — the context builder rewrites
            # it from request metadata at the top of every step.
            if item.trust is not TrustLevel.USER:
                return item.trust
            break
    else:
        return TrustLevel.EXTERNAL_UNTRUSTED
    calls_by_id: dict[str, list[ToolCallItem]] = {}
    if run_kind == "skill_review":
        for candidate in active_turn:
            if isinstance(candidate, ToolCallItem):
                calls_by_id.setdefault(candidate.call_id, []).append(candidate)
    review_skill_load_calls = {
        call_id
        for call_id, calls in calls_by_id.items()
        if len(calls) == 1 and calls[0].name == SKILL_LOAD_TOOL_NAME
    }
    for active_item in active_turn:
        if isinstance(active_item, ToolResultItem) and active_item.trust in {
            TrustLevel.EXTERNAL_UNTRUSTED,
            TrustLevel.KNOWLEDGE,
        }:
            if run_kind == "skill_review" and active_item.call_id in review_skill_load_calls:
                continue
            return TrustLevel.EXTERNAL_UNTRUSTED
        if (
            isinstance(active_item, ToolResultItem)
            and active_item.trust is TrustLevel.MEMORY
            and origin_trust is TrustLevel.USER
        ):
            origin_trust = TrustLevel.MEMORY
    return origin_trust


def _argument_trust(arguments: dict[str, Any], checkpoint: RunCheckpoint) -> dict[str, TrustLevel]:
    """Raise long verbatim values only when the active user message supplied them."""

    trust = dict.fromkeys(arguments, TrustLevel.EXTERNAL_UNTRUSTED)
    current_user = next(
        (
            item
            for item in reversed(checkpoint.conversation)
            if isinstance(item, UserMessage) and item.trust is TrustLevel.USER
        ),
        None,
    )
    if current_user is None:
        return trust
    user_text = [part.text for part in current_user.content if isinstance(part, TextPart)]
    for name, value in arguments.items():
        if isinstance(value, str) and len(value) >= 16 and any(value in text for text in user_text):
            trust[name] = TrustLevel.USER
    return trust


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
        artifact_writers: ArtifactWriterProvider | None = None,
        credentials: CredentialResolver | None = None,
        current_principal: Principal | None = None,
        max_parallel_calls: int = 8,
        hard_ceiling_multiplier: int = 4,
        maximum_loaded_skills: int = 2,
        maximum_skill_body_tokens: int = 6_000,
        approval_expiry_seconds: Mapping[RiskLevel, int] | None = None,
        standing_authorizer: StandingAuthorizer | None = None,
        delegations: DelegationStarter | None = None,
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
        self._artifact_writers = artifact_writers
        self._credentials = credentials or UnavailableCredentialResolver()
        self._current_principal = current_principal
        self._max_parallel_calls = max_parallel_calls
        if hard_ceiling_multiplier < 1:
            raise ValueError("hard ceiling multiplier must be positive")
        self._hard_ceiling_multiplier = hard_ceiling_multiplier
        if maximum_loaded_skills <= 0 or maximum_skill_body_tokens <= 0:
            raise ValueError("skill body limits must be positive")
        self._maximum_loaded_skills = maximum_loaded_skills
        self._maximum_skill_body_tokens = maximum_skill_body_tokens
        self._approval_expiry_seconds = dict(
            approval_expiry_seconds
            or {
                RiskLevel.LOW: 86_400,
                RiskLevel.MEDIUM: 86_400,
                RiskLevel.HIGH: 14_400,
                RiskLevel.CRITICAL: 3_600,
            }
        )
        self._standing_authorizer = standing_authorizer
        self._delegations = delegations
        self._key_locks: dict[str, _KeyLockEntry] = {}
        self._key_locks_guard = asyncio.Lock()
        self._completed_traces: deque[PipelineTrace] = deque(maxlen=1_024)

    def completed_traces(self, run_id: UUID) -> tuple[PipelineTrace, ...]:
        return tuple(trace for trace in self._completed_traces if trace.run_id == run_id)

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
        if self._parallel_ok(tool_calls, run, checkpoint, principal, agent):
            token.raise_if_cancelled()
            parallel_group = self._ids.new_id()
            settled = await asyncio.gather(
                *(
                    self._dispatch_one(
                        run=run,
                        checkpoint=checkpoint,
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
                    checkpoint=checkpoint,
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
        checkpoint: RunCheckpoint,
        call: ToolCallItem,
        principal: Principal,
        step: Step,
        agent: AgentSpec,
        token: CancellationToken,
        lease: WorkerLease | None,
        parallel_group: UUID | None,
    ) -> ToolResultItem:
        progress: list[int] = []
        try:
            tool = self._resolve_pinned_tool(checkpoint, call.name, principal)
        except NotFoundError:
            return await self._refusal(
                run,
                call,
                "policy.matrix.unknown_tool",
                ToolOutcomeStatus.DENIED,
                lease,
            )
        progress.append(1)
        enabled_names = (
            frozenset(checkpoint.pinned_tool_names)
            if tool.spec.source is ToolSource.MCP and checkpoint.pinned_tool_names
            else frozenset(agent.enabled_tools)
        )
        if call.name not in enabled_names:
            return await self._refusal(
                run,
                call,
                "policy.matrix.unknown_tool",
                ToolOutcomeStatus.DENIED,
                lease,
            )
        progress.append(2)
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
        progress.append(3)
        if call.parse_error is not None:
            return await self._refusal(
                run, call, "tool.arguments_invalid", ToolOutcomeStatus.FAILED, lease
            )
        progress.append(4)
        try:
            arguments, _rendered, arguments_hash = validate_and_normalize(
                call.arguments, tool.spec.input_schema
            )
        except ToolValidationError:
            return await self._refusal(
                run, call, "tool.arguments_invalid", ToolOutcomeStatus.FAILED, lease
            )
        progress.append(5)

        async with self._uow_factory() as uow:
            uncertain_prior = await uow.invocations.has_uncertain_non_idempotent(
                run.id,
                tool_name=tool.spec.name,
                normalized_arguments_hash=arguments_hash,
                principal=principal,
            )
        if uncertain_prior:
            return await self._refusal(
                run,
                call,
                "tool.outcome_unknown",
                ToolOutcomeStatus.DENIED,
                lease,
                message=(
                    "Not performed. An identical prior call has an unknown outcome and "
                    "must not be repeated."
                ),
            )

        key = _idempotency_key(run, step, call, tool, arguments_hash)
        result_item = await self._execute_once(
            run=run,
            checkpoint=checkpoint,
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
            progress=progress,
        )
        if not result_item.is_error and tuple(progress) == PIPELINE_STEP_SEQUENCE:
            self._completed_traces.append(
                PipelineTrace(
                    run_id=run.id,
                    call_id=call.call_id,
                    tool_name=tool.spec.name,
                    tool_source=tool.spec.source,
                    steps=tuple(progress),
                )
            )
        return result_item

    async def _execute_once(
        self,
        *,
        run: Run,
        checkpoint: RunCheckpoint,
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
        progress: list[int],
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
                origin_trust=_turn_origin_trust(checkpoint, run.kind.value),
                parallel_group=parallel_group,
                created_at=now,
                updated_at=now,
            )
        if not approval_granted and decision is None:
            action = self._proposed_action(
                run,
                checkpoint,
                step,
                tool,
                candidate,
                arguments,
                arguments_hash,
            )
            decision = await self._policy.evaluate(action, principal, run)
            progress.extend((6, 7))
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
                    checkpoint=checkpoint,
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
                    progress=progress,
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
            if invocation.outcome.status is ToolOutcomeStatus.SUCCEEDED:
                self._apply_context_update(
                    checkpoint,
                    self._prepare_context_update(
                        checkpoint,
                        tool.spec,
                        invocation.structured_result,
                    ),
                )
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
                checkpoint=checkpoint,
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
                progress=progress,
            )
        if recovery not in {
            ToolRecoveryAction.RESUME_AUTHORIZATION,
            ToolRecoveryAction.REEXECUTE,
            ToolRecoveryAction.REPLAY_IDEMPOTENCY_KEY,
        }:
            raise AssertionError(f"unhandled tool recovery action {recovery.value}")

        standing_authorization: StandingAuthorization | None = None
        if not approval_granted:
            if decision is None:
                raise AssertionError("policy decision was not evaluated")
            invocation = invocation.model_copy(update={"policy_decision": decision}, deep=True)
            if decision.decision is PolicyDecisionType.DENY:
                return await self._deny_invocation(
                    run, call, tool, invocation, decision.reason_code, lease
                )
            if decision.decision is PolicyDecisionType.REQUIRE_APPROVAL:
                standing_authorization = await self._standing_authorization(
                    run=run,
                    checkpoint=checkpoint,
                    principal=principal,
                    agent=agent,
                    step=step,
                    tool=tool,
                    invocation=invocation,
                    arguments=arguments,
                    arguments_hash=arguments_hash,
                    decision=decision,
                )
                if standing_authorization is None or not standing_authorization.allowed:
                    approval = await self._request_approval(
                        run, call, principal, agent, tool, invocation, decision, lease
                    )
                    raise ApprovalRequiredError(approval.id)

        if invocation.status is ToolInvocationStatus.PROPOSED:
            async with self._uow_factory() as uow:
                invocation = await authorize_tool_invocation(
                    uow.invocations, invocation, self._clock, lease
                )
                authorization_payload: dict[str, object] = {
                    "name": call.name,
                    "call_id": call.call_id,
                }
                if standing_authorization is not None:
                    authorization_payload.update(
                        {
                            "authorization_kind": standing_authorization.authorization_kind,
                            "authorization_ref": standing_authorization.authorization_ref,
                            "authorization_reason": standing_authorization.reason_code,
                        }
                    )
                await self._event_in(
                    uow,
                    run,
                    "tool.call.authorized",
                    authorization_payload,
                    lease,
                )
        if invocation.status is ToolInvocationStatus.AUTHORIZED and progress[-1] == 7:
            progress.append(8)
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
        if invocation.status is ToolInvocationStatus.RUNNING and progress[-1] == 8:
            progress.append(9)
        if invocation.status is not ToolInvocationStatus.RUNNING:
            raise ConflictError(f"cannot recover tool invocation in {invocation.status.value}")

        if tool.spec.name == "conversation.ask_user":
            question_id = invocation.id
            if invocation.suspended_kind is None:
                suspended = invocation.model_copy(
                    update={
                        "suspended_kind": "user_input",
                        "suspended_ref": str(question_id),
                        "updated_at": self._clock.now(),
                    },
                    deep=True,
                )
                async with self._uow_factory() as uow:
                    invocation = await uow.invocations.transition(
                        invocation.id,
                        ToolInvocationStatus.RUNNING,
                        suspended,
                        lease=lease,
                    )
            elif invocation.suspended_kind != "user_input" or invocation.suspended_ref != str(
                question_id
            ):
                raise ConflictError("ask-user invocation has an invalid suspension marker")
            raise UserInputRequiredError(question_id, invocation.id)

        if tool.spec.name == DELEGATE_RUN_TOOL_NAME:
            if invocation.suspended_kind not in (None, "child_run"):
                raise ConflictError("delegate invocation has an invalid suspension marker")
            if self._delegations is None:
                return await self._finish(
                    run,
                    call,
                    tool,
                    invocation,
                    ToolResult(
                        ok=False,
                        content=[],
                        failure=ToolFailure(
                            kind=ToolFailureKind.TRANSPORT,
                            reason_code="tool.unavailable",
                            detail="delegation is not enabled in this deployment",
                            retryable=False,
                        ),
                    ),
                    lease,
                )
            try:
                delegation_request = DelegationRequest.model_validate(arguments)
            except ValueError as error:
                return await self._finish(
                    run,
                    call,
                    tool,
                    invocation,
                    ToolResult(
                        ok=False,
                        content=[],
                        failure=ToolFailure(
                            kind=ToolFailureKind.INVALID_ARGUMENTS,
                            reason_code="delegation.brief_invalid",
                            detail=f"delegation request failed validation: {error}",
                            retryable=False,
                        ),
                    ),
                    lease,
                )
            try:
                delegation = await self._delegations.materialize(
                    request=delegation_request,
                    run=run,
                    agent=agent,
                    principal=principal,
                    invocation=invocation,
                    pinned_tools=checkpoint.pinned_tool_specs,
                    lease=lease,
                )
            except DelegationValidationError as error:
                return await self._finish(
                    run,
                    call,
                    tool,
                    invocation,
                    ToolResult(
                        ok=False,
                        content=[],
                        failure=ToolFailure(
                            kind=ToolFailureKind.INVALID_ARGUMENTS,
                            reason_code=error.reason,
                            detail=str(error),
                            retryable=False,
                        ),
                    ),
                    lease,
                )
            raise ChildRunRequiredError(
                delegation.id,
                invocation.id,
                [
                    child.child_run_id
                    for child in delegation.children
                    if child.child_run_id is not None
                ],
            )

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

        async def bridge_dispatch(
            name: str, bridge_arguments: dict[str, Any], bridge_call_id: str
        ) -> dict[str, Any]:
            bridge_call = ToolCallItem(
                call_id=bridge_call_id,
                item_index=0,
                name=name,
                arguments=bridge_arguments,
                raw_arguments=json.dumps(
                    bridge_arguments, ensure_ascii=False, separators=(",", ":")
                ),
            )
            approval_poll_seconds = 0.2
            while True:
                token.raise_if_cancelled()
                try:
                    item = await self._dispatch_one(
                        run=run,
                        checkpoint=checkpoint,
                        call=bridge_call,
                        principal=principal,
                        step=step,
                        agent=agent,
                        token=token,
                        lease=lease,
                        parallel_group=None,
                    )
                    break
                except ApprovalRequiredError:
                    await asyncio.sleep(approval_poll_seconds)
                    approval_poll_seconds = min(2.0, approval_poll_seconds * 2)
            if not item.is_error:
                return {
                    "status": "succeeded",
                    "result": [part.model_dump(mode="json") for part in item.content],
                    "retryable": False,
                }
            if item.content and isinstance(item.content[0], TextPart):
                try:
                    outcome = ToolOutcome.model_validate_json(item.content[0].text)
                except ValueError:
                    pass
                else:
                    return {
                        "status": outcome.status.value,
                        "reason_code": outcome.reason_code,
                        "retryable": outcome.retryable,
                    }
            return {
                "status": "failed",
                "reason_code": "bridge.invalid_tool_result",
                "retryable": False,
            }

        deadline = self._clock.now() + timedelta(seconds=tool.spec.timeout_seconds)
        if run.deadline_at is not None:
            deadline = min(deadline, run.deadline_at)
        effective_timeout = max(0.0, (deadline - self._clock.now()).total_seconds())
        execution_context = ToolExecutionContext(
            invocation_id=invocation.id,
            call_id=call.call_id,
            run_id=run.id,
            session_id=run.session_id,
            tenant_id=run.tenant_id,
            principal=principal,
            step_number=step.step_number,
            attempt_number=1,
            lease_epoch=0 if lease is None else lease.lease_epoch,
            idempotency_key=key,
            deadline_at=deadline,
            timeout_seconds=effective_timeout,
            maximum_output_bytes=tool.spec.maximum_output_bytes,
            target=ExecutionTarget(
                kind=tool.spec.target_kind,
                isolated=tool.spec.target_kind in {"sandbox", "browser_provider"},
                network_enabled=tool.spec.target_kind in {"web_provider", "browser_provider"},
                device_id=tool.spec.device_id,
            ),
            workspace=(
                None
                if self._workspace_factory is None
                or (
                    tool.spec.target_kind != "sandbox"
                    and tool.spec.side_effect
                    not in {SideEffectClass.WORKSPACE_READ, SideEffectClass.WORKSPACE_WRITE}
                )
                else self._workspace_factory.for_run(
                    run.tenant_id, run.id, 0 if lease is None else lease.lease_epoch
                )
            ),
            artifacts=(
                _UnavailableCollaborator()
                if self._artifact_writers is None
                else self._artifact_writers.for_run(
                    tenant_id=run.tenant_id,
                    principal_id=principal.principal_id,
                    session_id=run.session_id,
                    run_id=run.id,
                    origin=_writer_origin(tool),
                )
            ),
            credentials=(
                UnavailableCredentialResolver()
                if tool.spec.target_kind == "sandbox"
                else self._credentials
            ),
            bridge_dispatch=(bridge_dispatch if tool.spec.target_kind == "sandbox" else None),
            working_state=deepcopy(checkpoint.working_state),
            loaded_skills=tuple(body.model_dump(mode="json") for body in checkpoint.loaded_skills),
            available_tools=frozenset(checkpoint.pinned_tool_names or agent.enabled_tools),
            origin_trust=invocation.origin_trust,
            argument_trust=_argument_trust(arguments, checkpoint),
            run_kind=run.kind.value,
            cancellation=token,
            mark_effect_sent=mark_effect_sent,
        )
        if self._uow_factory.is_open():
            raise RuntimeError("tool execution cannot begin while a unit of work is open")
        if progress[-1] == 9:
            progress.append(10)
        try:
            if effective_timeout <= 0:
                raise TimeoutError
            if tool.spec.side_effect is not SideEffectClass.NONE:
                await mark_effect_sent()
            async with asyncio.timeout(effective_timeout):
                try:
                    result = await tool.execute(arguments, execution_context)
                except ToolTrustRejectedError:
                    result = ToolResult(
                        ok=False,
                        content=[],
                        failure=ToolFailure(
                            kind=ToolFailureKind.INVALID_ARGUMENTS,
                            reason_code="tool.trust_rejected",
                            detail="tool rejected content with insufficient provenance trust",
                            retryable=False,
                        ),
                    )
                except ToolValidationError:
                    result = ToolResult(
                        ok=False,
                        content=[],
                        failure=ToolFailure(
                            kind=ToolFailureKind.INVALID_ARGUMENTS,
                            reason_code="tool.arguments_invalid",
                            detail="tool rejected the validated arguments",
                            retryable=False,
                        ),
                    )
            if result.ok:
                validate_output(result.structured, tool.spec.output_schema)
                if progress[-1] == 10:
                    progress.append(11)
            if result.ok:
                result = await self._artifactize_large_output(
                    result=result,
                    tool=tool,
                    run=run,
                    principal=principal,
                )
                if progress[-1] == 11:
                    progress.append(12)
            if result.ok:
                validate_output(result.structured, tool.spec.output_schema)
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
        prepared_update = _PreparedContextUpdate()
        if result.ok:
            try:
                prepared_update = self._prepare_context_update(
                    checkpoint,
                    tool.spec,
                    result.structured,
                )
            except (ConflictError, ValueError):
                result = ToolResult(
                    ok=False,
                    content=[],
                    failure=ToolFailure(
                        kind=ToolFailureKind.OUTPUT_INVALID,
                        reason_code="tool.output_invalid",
                        detail="control tool returned an invalid context update",
                        retryable=False,
                    ),
                )
        result_item = await self._finish(run, call, tool, invocation, result, lease)
        if progress[-1] == 12:
            progress.extend((13, 14))
        if result.ok:
            self._apply_context_update(checkpoint, prepared_update)
        return result_item

    async def _artifactize_large_output(
        self,
        *,
        result: ToolResult,
        tool: Tool,
        run: Run,
        principal: Principal,
    ) -> ToolResult:
        rendered = json.dumps(
            [part.model_dump(mode="json") for part in result.content],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(rendered) <= tool.spec.maximum_output_bytes:
            result.metrics["output_bytes"] = len(rendered)
            return result
        if self._artifact_writers is None:
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.OUTPUT_TOO_LARGE,
                    reason_code="tool.output_invalid",
                    detail="tool output exceeded its declared byte limit",
                    retryable=False,
                ),
            )
        hard_ceiling = tool.spec.maximum_output_bytes * self._hard_ceiling_multiplier
        artifact_bytes = rendered[:hard_ceiling]
        partial_capture = len(artifact_bytes) < len(rendered)
        budget = tool.spec.maximum_output_bytes
        structured = None if result.structured is None else dict(result.structured)
        if structured is not None:
            for key in ("stdout", "stderr"):
                value = structured.get(key)
                if isinstance(value, str) and len(value.encode("utf-8")) > budget // 2:
                    structured[key] = (
                        value.encode("utf-8")[: budget // 4].decode("utf-8", errors="ignore")
                        + "\n[TRUNCATED]"
                    )
        # Validate the bounded structured candidate before creating a durable
        # artifact, so a schema failure cannot leave an unreferenced object.
        validate_output(structured, tool.spec.output_schema)

        async def stream() -> AsyncIterator[bytes]:
            yield artifact_bytes

        trust = _effective_output_trust(result, tool)
        writer = self._artifact_writers.for_run(
            tenant_id=run.tenant_id,
            principal_id=principal.principal_id,
            session_id=run.session_id,
            run_id=run.id,
            origin=ArtifactOrigin.TOOL_OUTPUT,
        )
        filename = f"{tool.spec.name.replace('.', '-')}-output"
        filename += ".partial.json" if partial_capture else ".json"
        media_type = "application/octet-stream" if partial_capture else "application/json"
        ref = await writer.create(stream(), filename, media_type, trust)
        capture_label = (
            f"captured first {len(artifact_bytes):,} of {len(rendered):,} bytes"
            if partial_capture
            else "full output"
        )
        provisional_marker = (
            f"\n[... {len(rendered):,} bytes elided; {capture_label}: "
            f"artifact:{ref.artifact_id} ...]\n"
        ).encode()
        excerpt_budget = max(0, budget - len(provisional_marker))
        head_bytes = int(excerpt_budget * 0.75)
        tail_bytes = excerpt_budget - head_bytes
        elided = max(0, len(rendered) - head_bytes - tail_bytes)
        marker = (
            f"\n[... {elided:,} bytes elided; {capture_label}: artifact:{ref.artifact_id} ...]\n"
        ).encode()
        if len(marker) > budget:
            excerpt = marker[:budget]
        else:
            remaining = budget - len(marker)
            head_bytes = min(head_bytes, remaining)
            tail_bytes = min(tail_bytes, remaining - head_bytes)
            excerpt = (
                rendered[:head_bytes] + marker + (rendered[-tail_bytes:] if tail_bytes else b"")
            )
        return result.model_copy(
            update={
                "content": [
                    TextPart(text=excerpt.decode("utf-8", errors="ignore")),
                    FileReferencePart(
                        artifact_id=ref.artifact_id,
                        media_type=ref.media_type,
                        filename=filename,
                    ),
                ],
                "structured": structured,
                "artifacts": [
                    *result.artifacts,
                    {
                        "artifact_id": str(ref.artifact_id),
                        "sha256": ref.sha256,
                        "size_bytes": ref.size_bytes,
                        "media_type": ref.media_type,
                        "role": "truncated_output",
                    },
                ],
                "metrics": {
                    "output_bytes": len(rendered),
                    "captured_bytes": len(artifact_bytes),
                    "discarded_bytes": len(rendered) - len(artifact_bytes),
                    "truncated": 1,
                },
            },
            deep=True,
        )

    def _proposed_action(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        step: Step,
        tool: Tool,
        invocation: ToolInvocation,
        arguments: dict[str, Any],
        arguments_hash: str,
    ) -> ProposedAction:
        return ProposedAction(
            kind=_action_kind(tool),
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
            argument_trust=_argument_trust(arguments, checkpoint),
            origin_trust=invocation.origin_trust,
            target=ExecutionTarget(
                kind=tool.spec.target_kind,
                isolated=tool.spec.target_kind in {"sandbox", "browser_provider"},
                network_enabled=tool.spec.target_kind in {"web_provider", "browser_provider"},
                device_id=tool.spec.device_id,
                server_id=tool.spec.server_id,
            ),
            evaluated_at=self._clock.now(),
        )

    async def _standing_authorization(
        self,
        *,
        run: Run,
        checkpoint: RunCheckpoint,
        principal: Principal,
        agent: AgentSpec,
        step: Step,
        tool: Tool,
        invocation: ToolInvocation,
        arguments: dict[str, Any],
        arguments_hash: str,
        decision: PolicyDecision,
    ) -> StandingAuthorization | None:
        if self._standing_authorizer is None:
            return None
        action = self._proposed_action(
            run,
            checkpoint,
            step,
            tool,
            invocation,
            arguments,
            arguments_hash,
        )
        deadline = self._clock.now() + timedelta(seconds=tool.spec.timeout_seconds)
        if run.deadline_at is not None:
            deadline = min(deadline, run.deadline_at)
        try:
            authorization = await self._standing_authorizer.authorize(
                action=action,
                decision=decision,
                principal=principal,
                run=run,
                agent_version=agent.version,
                action_deadline=deadline,
            )
        except Exception:
            logger.exception(
                "standing_authorization_failed",
                extra={"tool_name": tool.spec.name},
            )
            return None
        if authorization.allowed and (
            authorization.authorization_kind is None or authorization.authorization_ref is None
        ):
            logger.error(
                "standing_authorization_missing_evidence",
                extra={"tool_name": tool.spec.name},
            )
            return None
        return authorization

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
        action_summary = f"Run {tool.spec.name} with validated arguments."
        approval_arguments = dict(invocation.normalized_arguments)
        approval_view = getattr(tool, "approval_view", None)
        if approval_view is not None:
            try:
                action_summary, approval_arguments = await approval_view(
                    approval_arguments,
                    tenant_id=run.tenant_id,
                )
            except (RunCancelledError, BudgetExceededError):
                raise
            except Exception:
                logger.exception(
                    "approval_view_failed",
                    extra={"tool_name": tool.spec.name},
                )
                action_summary = f"Run {tool.spec.name} with validated arguments."
                approval_arguments = dict(invocation.normalized_arguments)
        approval = ApprovalRequest(
            id=self._ids.new_id(),
            tenant_id=run.tenant_id,
            principal_id=principal.principal_id,
            session_id=run.session_id,
            run_id=run.id,
            action_kind=_action_kind(tool),
            action_id=invocation.id,
            tool_invocation_id=invocation.id,
            status=ApprovalStatus.PENDING,
            action_summary=action_summary,
            tool_name=tool.spec.name,
            arguments=_approval_argument_view(approval_arguments),
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
        checkpoint: RunCheckpoint,
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
        progress: list[int],
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
        action = self._proposed_action(
            run,
            checkpoint,
            step,
            tool,
            invocation,
            arguments,
            arguments_hash,
        )
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
            checkpoint=checkpoint,
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
            progress=progress,
            lock_acquired=True,
        )

    def _parallel_ok(
        self,
        calls: list[ToolCallItem],
        run: Run,
        checkpoint: RunCheckpoint,
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
        enabled_names: frozenset[str]
        for call in calls:
            try:
                tool = self._resolve_pinned_tool(checkpoint, call.name, principal)
            except NotFoundError:
                return False
            enabled_names = (
                frozenset(checkpoint.pinned_tool_names)
                if tool.spec.source is ToolSource.MCP and checkpoint.pinned_tool_names
                else frozenset(agent.enabled_tools)
            )
            if (
                call.name not in enabled_names
                or not tool.spec.required_scopes.issubset(principal.scopes)
                or tool.spec.side_effect not in read_only_effects
                or not tool.spec.allow_parallel
                or tool.spec.idempotency is not IdempotencyClass.READ_ONLY
            ):
                return False
        return True

    def _resolve_pinned_tool(
        self,
        checkpoint: RunCheckpoint,
        name: str,
        principal: Principal,
    ) -> Tool:
        pinned = checkpoint.pinned_tool_specs.get(name)
        return self._registry.get(
            name,
            pinned.version if pinned is not None else checkpoint.pinned_tool_versions.get(name),
            tenant_id=principal.tenant_id,
            source=None if pinned is None else pinned.source,
            server_id=None if pinned is None else pinned.server_id,
        )

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

    def _prepare_context_update(
        self,
        checkpoint: RunCheckpoint,
        tool_spec: ToolSpec,
        structured: dict[str, Any] | None,
    ) -> _PreparedContextUpdate:
        if tool_spec.name not in {WORKING_STATE_TOOL_NAME, SKILL_LOAD_TOOL_NAME}:
            return _PreparedContextUpdate()
        if tool_spec.source is not ToolSource.BUILTIN or tool_spec.kind is not ToolKind.CONTROL:
            raise ConflictError("context updates require a trusted builtin control tool")
        if tool_spec.name == SKILL_LOAD_TOOL_NAME:
            if structured is None:
                raise ConflictError("persisted skill-load result has no structured output")
            update = structured.get("skill_update")
            if not isinstance(update, dict):
                raise ConflictError("persisted skill-load result has no update mapping")
            operation = update.get("operation")
            name = update.get("name")
            if operation == "unload" and isinstance(name, str):
                return _PreparedContextUpdate(
                    loaded_skills=tuple(
                        body for body in checkpoint.loaded_skills if body.name != name
                    )
                )
            raw_body = update.get("body")
            if operation != "load" or not isinstance(raw_body, dict):
                raise ConflictError("persisted skill-load update is malformed")
            body = LoadedSkillBody.model_validate(raw_body)
            retained = [
                candidate for candidate in checkpoint.loaded_skills if candidate.name != body.name
            ]
            if len(retained) >= self._maximum_loaded_skills:
                raise ConflictError("persisted skill-load update exceeds the loaded-skill cap")
            retained.append(body)
            if sum(candidate.tokens for candidate in retained) > self._maximum_skill_body_tokens:
                raise ConflictError("persisted skill-load update exceeds the skill-body token cap")
            return _PreparedContextUpdate(loaded_skills=tuple(retained))
        if structured is None:
            return _PreparedContextUpdate()
        if structured.get("updated") is True:
            state = structured.get("working_state")
            if not isinstance(state, dict):
                raise ConflictError("persisted working-state result has no state mapping")
            return _PreparedContextUpdate(working_state=dict(state))
        return _PreparedContextUpdate()

    @staticmethod
    def _apply_context_update(
        checkpoint: RunCheckpoint,
        prepared: _PreparedContextUpdate,
    ) -> None:
        if prepared.loaded_skills is not None:
            checkpoint.loaded_skills = list(prepared.loaded_skills)
        if prepared.working_state is not None:
            checkpoint.working_state["context"] = dict(prepared.working_state)

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
            unavailable = result.failure.kind is ToolFailureKind.TRANSPORT
            uncertain = result.failure.kind is ToolFailureKind.OUTCOME_UNKNOWN
            outcome = ToolOutcome(
                status=(
                    ToolOutcomeStatus.UNCERTAIN
                    if uncertain
                    else ToolOutcomeStatus.UNAVAILABLE
                    if unavailable
                    else ToolOutcomeStatus.FAILED
                ),
                action=call.name,
                reason_code=result.failure.reason_code,
                message=message_for(result.failure.reason_code),
                retryable=result.failure.retryable,
                remediation=(
                    "modify_arguments"
                    if not unavailable
                    and (
                        result.failure.retryable
                        or result.failure.kind
                        in {ToolFailureKind.INVALID_ARGUMENTS, ToolFailureKind.NOT_FOUND}
                    )
                    else "none"
                ),
            )
            status = ToolInvocationStatus.UNCERTAIN if uncertain else ToolInvocationStatus.FAILED
            event_type = "tool.call.uncertain" if uncertain else "tool.call.failed"
        if result.ok:
            trust = _effective_output_trust(result, tool)
            result_item = ToolResultItem(call_id=call.call_id, content=result.content, trust=trust)
        else:
            result_item = _outcome_item(
                call.call_id,
                outcome,
                tool.spec.output_trust,
                result.failure.external_text if result.failure is not None else None,
            )
        artifact_id: UUID | None = None
        artifact_record = next(
            (
                candidate
                for candidate in reversed(result.artifacts)
                if isinstance(candidate, dict) and candidate.get("role") == "truncated_output"
            ),
            next(
                (candidate for candidate in result.artifacts if isinstance(candidate, dict)),
                None,
            ),
        )
        if artifact_record is not None:
            raw_artifact_id = artifact_record.get("artifact_id")
            if isinstance(raw_artifact_id, str):
                try:
                    artifact_id = UUID(raw_artifact_id)
                except ValueError:
                    logger.warning(
                        "tool_artifact_id_malformed", extra={"tool_name": tool.spec.name}
                    )
        finished = invocation.model_copy(
            update={
                "status": status,
                "outcome": outcome,
                "result_item": result_item,
                "structured_result": result.structured,
                "output_bytes": result.metrics.get("output_bytes"),
                "truncated": bool(result.metrics.get("truncated", 0)),
                "artifact_id": artifact_id,
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
            if (
                tool.spec.name == WORKING_STATE_TOOL_NAME
                and result.ok
                and result.structured is not None
                and result.structured.get("updated") is True
            ):
                structured = result.structured
                state = structured.get("working_state")
                if not isinstance(state, dict):
                    raise ConflictError("working-state control result has no state mapping")
                await self._event_in(
                    uow,
                    run,
                    "context.working_state.updated",
                    {"working_state": state, "source": "control_tool"},
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
