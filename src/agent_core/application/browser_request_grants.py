"""One approval for a request on a signed-in website (ADR-0302).

The approval the owner resolved with ``approve_for_request`` is the grant. The
authorizer finds it through the run's ``browser.request_grant.created`` event,
checks the run, the login and the live page facts, and lets an ordinary
action through with a ``request`` dispatch constraint the isolated runtime
checks again. Every refusal falls back to an ordinary approval card.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from agent_core.application.browser_task_grants import read_task_grant_context
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalRequest, ApprovalResolutionType
from agent_core.domain.argument_views import approval_argument_view
from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserProfile,
    BrowserProfileStatus,
)
from agent_core.domain.browser_act_views import (
    describe_browser_action,
    request_grant_view_coverage,
    session_is_task_grant_eligible,
)
from agent_core.domain.browser_request_grants import (
    NO_REQUEST_GRANT,
    REQUEST_GRANT_AUTHORIZATION_KIND,
    REQUEST_GRANT_AUTHORIZED_REASON,
    REQUEST_GRANT_CREATED_EVENT,
    REQUEST_GRANT_DURATION,
    turn_reads_outside,
)
from agent_core.domain.errors import NotFoundError
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
)
from agent_core.domain.runs import Run
from agent_core.ports.browser import BrowserProvider, browser_snapshot_in_session
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.policies import PolicyEngine

logger = logging.getLogger(__name__)

# Above any event sequence: latest_before reads the newest matching event.
_NEWEST = 2**62
AUTHENTICATION_RESUMED_EVENT = "browser.authentication.resumed"


@dataclass(frozen=True, slots=True)
class RequestGrantResolution:
    """What the public approval service needs to allow a request: the clock
    that stamps the grant's window."""

    clock: Clock


async def request_approval_for_run(
    uow: RepositoryUnitOfWork, run: Run, principal: Principal
) -> ApprovalRequest | None:
    """The run's request approval, found through its creation event."""

    created = await uow.events.latest_before(
        run.session_id, _NEWEST, REQUEST_GRANT_CREATED_EVENT, principal, run_id=run.id
    )
    if created is None or created.run_id != run.id:
        return None
    try:
        approval = await uow.approvals.get(UUID(str(created.payload["approval_id"])), principal)
    except (KeyError, ValueError, NotFoundError):
        return None
    if (
        approval.run_id != run.id
        or approval.resolution is not ApprovalResolutionType.APPROVE_FOR_REQUEST
        or approval.request_grant_offer is None
        or approval.resolved_at is None
    ):
        return None
    return approval


async def _signed_in_by_this_run(
    uow: RepositoryUnitOfWork,
    run: Run,
    principal: Principal,
    profile: BrowserProfile,
    since: datetime,
) -> bool:
    """Whether the profile's newest sign-in began after ``since`` and is the
    one this run's own authentication wait resumed from (ADR-0164 decision 3)."""

    records = await uow.browser_authentications.list(principal, profile_id=profile.id)
    if not records:
        return False
    newest = max(records, key=lambda record: (record.created_at, record.id.int))
    if newest.created_at <= since:
        return False
    resumed = await uow.events.latest_before(
        run.session_id, _NEWEST, AUTHENTICATION_RESUMED_EVENT, principal, run_id=run.id
    )
    return (
        resumed is not None
        and resumed.run_id == run.id
        and resumed.payload.get("authentication_id") == str(newest.id)
        and resumed.payload.get("profile_id") == str(profile.id)
    )


class BrowserRequestGrantAuthorizer:
    """Authorize a browser.act the owner allowed for the whole request."""

    def __init__(
        self,
        *,
        provider: BrowserProvider,
        uow_factory: UnitOfWorkFactory,
        policy: PolicyEngine,
        now: Callable[[], datetime],
    ) -> None:
        """Keep the browser provider, store and policy the checks read."""

        self._provider = provider
        self._uow_factory = uow_factory
        self._policy = policy
        self._now = now

    async def authorize(
        self,
        *,
        action: ProposedAction,
        decision: PolicyDecision,
        principal: Principal,
        run: Run,
        agent_version: str,
        action_deadline: datetime,
        turn: AuthorizationTurn | None = None,
    ) -> StandingAuthorization:
        """Allow the action only when this run's request grant still covers it."""

        if (
            action.name != "browser.act"
            or action.target.kind != "browser_provider"
            or decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL
        ):
            return StandingAuthorization(allowed=False, reason_code=NO_REQUEST_GRANT)
        try:
            browser_action = BrowserAction.model_validate(action.arguments)
        except ValueError:
            return StandingAuthorization(allowed=False, reason_code=NO_REQUEST_GRANT)
        now = self._now()
        async with self._uow_factory() as uow:
            approval = await request_approval_for_run(uow, run, principal)
            if approval is None:
                return StandingAuthorization(allowed=False, reason_code=NO_REQUEST_GRANT)
            context = await read_task_grant_context(uow, run.session_id, principal, now=now)
            offer = approval.request_grant_offer
            assert offer is not None and approval.resolved_at is not None
            profile = context.profile
            session = context.session
            moved = (
                profile is not None
                and profile.generation != offer.profile_generation
                and await _signed_in_by_this_run(uow, run, principal, profile, approval.resolved_at)
            )
        expires_at = approval.resolved_at + REQUEST_GRANT_DURATION
        if session is None or not session_is_task_grant_eligible(
            run,
            session_tenant_id=session.tenant_id,
            session_principal_id=session.principal_id,
            session_metadata=session.metadata,
            owner_run_id=context.owner_run_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            turn=turn,
        ):
            return _not_covered(approval, "run_not_eligible")
        if now >= expires_at:
            return _not_covered(approval, "expired")
        if (
            profile is None
            or profile.id != offer.profile_id
            or profile.status is not BrowserProfileStatus.READY
            or not set(offer.origins) <= set(profile.allowed_origins)
            or (profile.generation != offer.profile_generation and not moved)
        ):
            return _not_covered(approval, "profile_changed")
        if agent_version != approval.agent_version:
            return _not_covered(approval, "agent_changed")
        snapshot = await browser_snapshot_in_session(self._provider, run.session_id)
        view = describe_browser_action(browser_action, snapshot)
        if not view.described or view.observation is None or view.element is None:
            return _not_covered(approval, "unavailable")
        revalidated = await self._policy.evaluate(
            action.model_copy(update={"evaluated_at": now}), principal, run
        )
        if (
            revalidated.decision is not PolicyDecisionType.REQUIRE_APPROVAL
            or revalidated.policy_version != decision.policy_version
            or revalidated.policy_version != approval.policy_version
        ):
            return _not_covered(approval, "policy_changed")
        free_text = not turn_reads_outside(turn)
        coverage = request_grant_view_coverage(
            view, browser_action, origins=offer.origins, free_text=free_text
        )
        if not coverage.covered:
            assert coverage.reason is not None
            return _not_covered(approval, coverage.reason)
        return StandingAuthorization(
            allowed=True,
            reason_code=REQUEST_GRANT_AUTHORIZED_REASON,
            authorization_kind=REQUEST_GRANT_AUTHORIZATION_KIND,
            authorization_ref=str(approval.id),
            authorization_view=_view(view.arguments),
            dispatch_constraint=BrowserDispatchConstraint(
                grant_kind="request",
                origins=offer.origins,
                path_prefix=None,
                not_after=min(expires_at, action_deadline),
                consequence_ceiling="unknown",
                max_text_characters=256,
                free_text=free_text,
            ),
        )


def _not_covered(approval: ApprovalRequest, reason: str) -> StandingAuthorization:
    """A refusal naming the closed reason and the approval that holds the grant."""

    code = f"browser.request_grant.{reason}"
    # Content-free: a closed reason and the approval that holds the grant.
    logger.info(
        "request_grant_not_covered", extra={"reason_code": code, "approval_id": str(approval.id)}
    )
    return StandingAuthorization(
        allowed=False,
        reason_code=code,
        authorization_kind=REQUEST_GRANT_AUTHORIZATION_KIND,
        authorization_ref=str(approval.id),
    )


def _view(arguments: dict[str, Any]) -> dict[str, Any]:
    """The action's arguments as an approval card shows them."""

    return approval_argument_view(arguments)
