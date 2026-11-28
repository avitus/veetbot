"""One approval for the scripts of a request (ADR-0342 decision 11).

The approval the owner resolved with ``approve_for_request`` is the grant. The
authorizer finds it through the run's ``script.request_grant.created`` event,
checks the run, the window and the policy, and lets a later script of that run
start without a card. Every refusal falls back to an ordinary approval. The
grant covers starting a script; each call a script makes over the bridge is
still decided by policy under its own trust.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from agent_core.application.browser_request_grants import approval_allowed_for_request
from agent_core.application.job_request_grants import run_allows_job_request_grant
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalRequest
from agent_core.domain.errors import NotFoundError
from agent_core.domain.execution import SANDBOX_RUN_CODE_TOOL_NAME
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run
from agent_core.domain.script_request_grants import (
    NO_SCRIPT_REQUEST_GRANT,
    SCRIPT_REQUEST_GRANT_AUTHORIZATION_KIND,
    SCRIPT_REQUEST_GRANT_AUTHORIZED_REASON,
    SCRIPT_REQUEST_GRANT_CREATED_EVENT,
    SCRIPT_REQUEST_GRANT_DURATION,
    SCRIPT_REQUEST_GRANT_SUMMARY,
    ScriptRequestGrantOffer,
)
from agent_core.domain.sessions import Session
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScriptRequestGrantResolution:
    """What the public approval service needs to allow a request's scripts: the
    clock that stamps the grant's window."""

    clock: Clock


def script_card_is_covered(tool_name: str, decision: PolicyDecision) -> bool:
    """The one card the grant stands in for: the script tool, asked about only
    because the turn has read outside content. A profile that asks before any
    code execution keeps asking."""

    return (
        tool_name == SANDBOX_RUN_CODE_TOOL_NAME
        and decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
        and decision.untrusted_escalation
    )


async def owner_request(
    uow: RepositoryUnitOfWork, run: Run, principal: Principal, turn: AuthorizationTurn | None
) -> bool:
    """A top-level interactive run of the owner's own chat, in a turn whose
    newest message is the owner's: the eligibility ADR-0344 set for jobs."""

    if turn is None or turn.newest_user_trust is not TrustLevel.USER:
        return False
    try:
        session: Session = await uow.sessions.get(run.session_id, principal)
    except NotFoundError:
        return False
    return run_allows_job_request_grant(run, session, principal)


async def script_request_grant_offer(
    uow: RepositoryUnitOfWork,
    *,
    tool_name: str,
    decision: PolicyDecision,
    run: Run,
    principal: Principal,
    turn: AuthorizationTurn | None,
) -> ScriptRequestGrantOffer | None:
    """The card's offer, or none: an owner-driven run of the owner's chat, and
    a card the grant would itself cover."""

    if not script_card_is_covered(tool_name, decision):
        return None
    if not await owner_request(uow, run, principal, turn):
        return None
    return ScriptRequestGrantOffer(summary=SCRIPT_REQUEST_GRANT_SUMMARY)


class ScriptRequestOffers:
    """Makes the offer for the tool pipeline, in its own unit of work."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        """Keep the store the eligibility check reads."""

        self._uow_factory = uow_factory

    async def offer(
        self,
        tool_name: str,
        decision: PolicyDecision,
        run: Run,
        principal: Principal,
        turn: AuthorizationTurn | None,
    ) -> ScriptRequestGrantOffer | None:
        """The offer for one pending card, or none."""

        if not script_card_is_covered(tool_name, decision):
            return None
        async with self._uow_factory() as uow:
            return await script_request_grant_offer(
                uow,
                tool_name=tool_name,
                decision=decision,
                run=run,
                principal=principal,
                turn=turn,
            )


async def script_request_approval_for_run(
    uow: RepositoryUnitOfWork, run: Run, principal: Principal
) -> ApprovalRequest | None:
    """The run's script request approval, found through its creation event."""

    approval = await approval_allowed_for_request(
        uow, run, principal, SCRIPT_REQUEST_GRANT_CREATED_EVENT
    )
    if (
        approval is None
        or approval.tool_name != SANDBOX_RUN_CODE_TOOL_NAME
        or not isinstance(approval.script_request_grant_offer, ScriptRequestGrantOffer)
    ):
        return None
    return approval


class ScriptRequestGrantAuthorizer:
    """Authorize a script the owner allowed for the whole request."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, now: Callable[[], datetime]) -> None:
        """Keep the store and the clock the checks read."""

        self._uow_factory = uow_factory
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
        """Allow the script only when this run's request grant covers it."""

        del action_deadline
        if not script_card_is_covered(action.name, decision):
            return StandingAuthorization(allowed=False, reason_code=NO_SCRIPT_REQUEST_GRANT)
        now = self._now()
        async with self._uow_factory() as uow:
            approval = await script_request_approval_for_run(uow, run, principal)
            if approval is None:
                return StandingAuthorization(allowed=False, reason_code=NO_SCRIPT_REQUEST_GRANT)
            assert approval.resolved_at is not None
            if not await owner_request(uow, run, principal, turn):
                return _not_covered(approval, "run_not_eligible")
        if now >= approval.resolved_at + SCRIPT_REQUEST_GRANT_DURATION:
            return _not_covered(approval, "expired")
        if agent_version != approval.agent_version:
            return _not_covered(approval, "agent_changed")
        if decision.policy_version != approval.policy_version:
            return _not_covered(approval, "policy_changed")
        return StandingAuthorization(
            allowed=True,
            reason_code=SCRIPT_REQUEST_GRANT_AUTHORIZED_REASON,
            authorization_kind=SCRIPT_REQUEST_GRANT_AUTHORIZATION_KIND,
            authorization_ref=str(approval.id),
        )


def _not_covered(approval: ApprovalRequest, reason: str) -> StandingAuthorization:
    """A refusal naming the closed reason and the approval that holds the grant."""

    code = f"script.request_grant.{reason}"
    # Content-free: a closed reason and the approval that holds the grant.
    logger.info(
        "script_request_grant_not_covered",
        extra={"reason_code": code, "approval_id": str(approval.id)},
    )
    return StandingAuthorization(
        allowed=False,
        reason_code=code,
        authorization_kind=SCRIPT_REQUEST_GRANT_AUTHORIZATION_KIND,
        authorization_ref=str(approval.id),
    )
