"""Perform one approved, revision-bound browser interaction."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.browser_act_views import (
    BROWSER_ACT_VIEW,
    UNDESCRIBED_SUMMARY,
    TaskGrantSessionContext,
    describe_browser_action,
    session_is_task_grant_eligible,
    task_grant_offer,
)
from agent_core.domain.browser_task_grants import BrowserTaskGrantScope, TaskGrantNotCovered
from agent_core.domain.policies import (
    AuthorizationTurn,
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.ports.browser import (
    GRANT_NOT_APPLICABLE,
    BrowserProvider,
    bind_browser_execution,
    browser_snapshot_in_session,
)
from agent_core.tools.browser_results import (
    OUTPUT_SCHEMA,
    browser_failure,
    observation_result,
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["click", "type", "select", "check", "press", "scroll"],
        },
        "expected_revision": {"type": "string", "minLength": 1, "maxLength": 128},
        "ref": {"type": "string", "minLength": 1, "maxLength": 128},
        "value": {"type": "string", "maxLength": 4096},
        "key": {
            "type": "string",
            "enum": [
                "Enter",
                "Escape",
                "Tab",
                "Space",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
            ],
        },
        "delta_y": {"type": "integer", "minimum": -2000, "maximum": 2000},
    },
    "required": ["kind", "expected_revision", "ref"],
    "additionalProperties": False,
}


TaskGrantContextReader = Callable[[UUID, Principal], Awaitable[TaskGrantSessionContext]]


class BrowserActApprovalPresenter:
    """Describe a pending browser action and, when every rule holds, offer a
    task grant (ADR-0129 section 6). Without the flag it never offers."""

    def __init__(
        self,
        provider: BrowserProvider,
        *,
        context_reader: TaskGrantContextReader | None = None,
        scopes: tuple[BrowserTaskGrantScope, ...] = (),
        enabled: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._context_reader = context_reader
        self._scopes = scopes
        self._enabled = enabled
        self._now = now

    async def present(
        self,
        arguments: dict[str, Any],
        *,
        run: Run,
        principal: Principal,
        turn: AuthorizationTurn | None,
        not_covered: TaskGrantNotCovered | None,
    ) -> ApprovalPresentation:
        del not_covered
        try:
            action = BrowserAction.model_validate(arguments)
        except ValidationError:
            return ApprovalPresentation(
                summary=UNDESCRIBED_SUMMARY,
                arguments={"view": BROWSER_ACT_VIEW, "described": False},
            )
        snapshot = await browser_snapshot_in_session(self._provider, run.session_id)
        view = describe_browser_action(action, snapshot)
        offer = None
        if self._enabled and view.described and self._context_reader is not None:
            context = await self._context_reader(run.session_id, principal)
            session = context.session
            eligible = session is not None and session_is_task_grant_eligible(
                run,
                session_tenant_id=session.tenant_id,
                session_principal_id=session.principal_id,
                session_metadata=session.metadata,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                turn=turn,
            )
            offer = task_grant_offer(
                enabled=True,
                view=view,
                action=action,
                scopes=self._scopes,
                eligible=eligible,
                turn=turn,
                profile=context.profile,
                active_grant=context.active_grant,
            )
        return ApprovalPresentation(
            summary=view.summary, arguments=view.arguments, task_grant_offer=offer
        )


class BrowserActTool:
    spec = ToolSpec(
        name="browser.act",
        version="1.0.0",
        description="Perform one approved action on an element from the current page revision.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_seconds=30,
        maximum_output_bytes=512 * 1024,
        allow_parallel=False,
        target_kind="browser_provider",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(
        self, provider: BrowserProvider, *, presenter: BrowserActApprovalPresenter | None = None
    ) -> None:
        self._provider = provider
        self._presenter = presenter or BrowserActApprovalPresenter(provider)

    async def approval_view_in_session(
        self,
        arguments: dict[str, Any],
        *,
        run: Run,
        principal: Principal,
        turn: AuthorizationTurn | None,
        not_covered: TaskGrantNotCovered | None,
    ) -> ApprovalPresentation:
        return await self._presenter.present(
            arguments, run=run, principal=principal, turn=turn, not_covered=not_covered
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            action = BrowserAction.model_validate(arguments)
        except ValidationError:
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                retryable=False,
            )
        try:
            # ADR-0129: a grant's constraint is checked before the watermark,
            # so a refusal never counts as a sent effect.
            constraint = _checked_constraint(self._provider, context.dispatch_constraint)
            await bind_browser_execution(self._provider, context)
            await context.mark_effect_sent()
            observation = await _act(self._provider, action, constraint)
        except BrowserProviderError as error:
            return browser_failure(error)
        return observation_result(self._provider, observation, self.spec.maximum_output_bytes)


def _checked_constraint(
    provider: BrowserProvider, constraint: BrowserDispatchConstraint | None
) -> BrowserDispatchConstraint | None:
    """Revalidate a grant's constraint before anything is marked as sent.

    One that fails its validator, or a provider whose runtime cannot recheck
    it against the live page, is refused before dispatch (ADR-0129).
    """

    if constraint is None:
        return None
    try:
        checked = BrowserDispatchConstraint.model_validate(constraint.model_dump())
    except (ValidationError, ValueError, TypeError) as exc:
        raise BrowserProviderError(GRANT_NOT_APPLICABLE, retryable=False) from exc
    if not _rechecks_constraints(provider):
        raise BrowserProviderError(GRANT_NOT_APPLICABLE, retryable=False)
    return checked


def _rechecks_constraints(provider: BrowserProvider) -> bool:
    try:
        parameter = inspect.signature(provider.act).parameters.get("constraint")
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind is inspect.Parameter.KEYWORD_ONLY


async def _act(
    provider: BrowserProvider,
    action: BrowserAction,
    constraint: BrowserDispatchConstraint | None,
) -> BrowserObservation:
    """Dispatch one action; an approved action carries no constraint."""

    if constraint is None:
        return await provider.act(action)
    constrained = cast(Callable[..., Awaitable[BrowserObservation]], provider.act)
    return await constrained(action, constraint=constraint)
