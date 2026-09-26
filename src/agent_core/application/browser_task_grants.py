"""Session-bound browser task grants (ADR-0129): authorization and composition."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.argument_views import approval_argument_view
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserDispatchConstraint,
    BrowserProfile,
    BrowserProfileStatus,
)
from agent_core.domain.browser_act_views import (
    TASK_GRANT_AUTHORIZATION_KIND,
    TASK_GRANT_AUTHORIZED_REASON,
    TaskGrantSessionContext,
    describe_browser_action,
    element_labels,
    option_texts,
    session_is_task_grant_eligible,
    turn_is_browser_only,
)
from agent_core.domain.browser_classification import task_grant_coverage
from agent_core.domain.browser_task_grants import (
    TASK_GRANT_MAX_TYPED_CHARACTERS,
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
    BrowserTaskGrantScope,
)
from agent_core.domain.errors import AgentCoreError, NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
)
from agent_core.domain.runs import Run
from agent_core.domain.sessions import SESSION_BROWSER_PROFILE_METADATA_KEY, Session
from agent_core.ports.browser import BrowserProvider, browser_snapshot_in_session
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.policies import PolicyEngine, StandingAuthorizer

logger = logging.getLogger(__name__)

# The denial when no task grant applies at all; not in the closed not-covered
# list, so the approval card records nothing about a grant.
NO_TASK_GRANT = "browser.task_grant.none"
_ENDED_REASONS = {
    BrowserTaskGrantEndReason.EXPIRED: "expired",
    BrowserTaskGrantEndReason.EXHAUSTED: "exhausted",
    BrowserTaskGrantEndReason.REVOKED: "revoked",
}


def _reason(name: str) -> str:
    return f"browser.task_grant.{name}"


def task_grant_ended_event(
    grant: BrowserTaskGrant, *, run_id: UUID | None, actor_type: str, actor_id: str | None = None
) -> NewEvent:
    """``browser.task_grant.ended``, once per grant (section 9)."""

    assert grant.end_reason is not None
    return NewEvent(
        session_id=grant.session_id,
        run_id=run_id,
        event_type="browser.task_grant.ended",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={
            "grant_id": str(grant.id),
            "reason": grant.end_reason.value,
            "actions_used": grant.actions_used,
            "typed_characters": grant.typed_characters,
        },
    )


async def read_task_grant_context(
    uow: RepositoryUnitOfWork, session_id: UUID, principal: Principal, *, now: datetime
) -> TaskGrantSessionContext:
    """The session, its bound profile and its active grant, as one principal
    may see them; anything missing reads as absent."""

    try:
        session: Session | None = await uow.sessions.get(session_id, principal)
    except NotFoundError:
        return TaskGrantSessionContext(session=None, profile=None)
    assert session is not None
    profile: BrowserProfile | None = None
    selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
    if isinstance(selected, str) and selected:
        try:
            profile = await uow.browser_profiles.get(UUID(selected), principal)
        except (NotFoundError, ValueError):
            profile = None
    grant = await uow.browser_task_grants.active_for_session(session_id, principal, now=now)
    return TaskGrantSessionContext(session=session, profile=profile, active_grant=grant)


class BrowserTaskGrantAuthorizer:
    """Authorize a browser.act the owner allowed for the task (section 5).

    Every denial falls back to the ordinary approval path. A denial that
    names the session's grant tells the card why the grant did not cover
    the action; a changed pin or a removed scope also ends the grant.
    """

    def __init__(
        self,
        *,
        provider: BrowserProvider,
        uow_factory: UnitOfWorkFactory,
        policy: PolicyEngine,
        scopes: tuple[BrowserTaskGrantScope, ...],
        now: Callable[[], datetime],
    ) -> None:
        self._provider = provider
        self._uow_factory = uow_factory
        self._policy = policy
        self._scopes = scopes
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
        del action_deadline
        if (
            action.name != "browser.act"
            or action.target.kind != "browser_provider"
            or decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL
        ):
            return StandingAuthorization(allowed=False, reason_code=NO_TASK_GRANT)
        try:
            browser_action = BrowserAction.model_validate(action.arguments)
        except ValueError:
            return StandingAuthorization(allowed=False, reason_code=NO_TASK_GRANT)
        now = self._now()
        async with self._uow_factory() as uow:
            context = await read_task_grant_context(uow, run.session_id, principal, now=now)
        grant, session = context.active_grant, context.session
        if grant is None or session is None:
            return StandingAuthorization(allowed=False, reason_code=NO_TASK_GRANT)
        if not session_is_task_grant_eligible(
            run,
            session_tenant_id=session.tenant_id,
            session_principal_id=session.principal_id,
            session_metadata=session.metadata,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            turn=turn,
        ):
            return _not_covered(grant, "run_not_eligible")
        if not any(
            (scope.origin, scope.path_prefix) == (grant.origin, grant.path_prefix)
            for scope in self._scopes
        ):
            await self._end(grant, principal, run, BrowserTaskGrantEndReason.SCOPE_REMOVED)
            return _not_covered(grant, "scope_removed")
        if not turn_is_browser_only(turn):
            return _not_covered(grant, "turn_not_browser_only")
        snapshot = await browser_snapshot_in_session(self._provider, run.session_id)
        view = describe_browser_action(browser_action, snapshot)
        if not view.described or view.observation is None or view.element is None:
            return _not_covered(grant, "unavailable")
        revalidated = await self._policy.evaluate(
            action.model_copy(update={"evaluated_at": now}), principal, run
        )
        if (
            revalidated.decision is not PolicyDecisionType.REQUIRE_APPROVAL
            or revalidated.policy_version != decision.policy_version
        ):
            return _not_covered(grant, "policy_changed")
        pin = _changed_pin(grant, context.profile, agent_version, revalidated.policy_version)
        if pin is not None:
            await self._end(grant, principal, run, pin)
            return _not_covered(grant, pin.value)
        coverage = task_grant_coverage(
            action=browser_action,
            page_url=view.observation.url,
            role=view.element.role,
            labels=element_labels(view.element, view.facts),
            facts=view.facts,
            option_texts=option_texts(browser_action),
            origin=grant.origin,
            path_prefix=grant.path_prefix,
        )
        if not coverage.covered:
            assert coverage.reason is not None
            return _not_covered(grant, coverage.reason)
        typed = (
            len(browser_action.value or "") if browser_action.kind is BrowserActionKind.TYPE else 0
        )
        async with self._uow_factory() as uow:
            used = await uow.browser_task_grants.consume(
                grant.id, principal, session_id=run.session_id, typed=typed, now=now
            )
            if used is None:
                current = await uow.browser_task_grants.get(grant.id, principal)
                return _not_covered(current, _unusable(current, typed, now))
            if used.end_reason is BrowserTaskGrantEndReason.EXHAUSTED:
                await uow.events.append(
                    task_grant_ended_event(used, run_id=run.id, actor_type="runtime")
                )
        return StandingAuthorization(
            allowed=True,
            reason_code=TASK_GRANT_AUTHORIZED_REASON,
            authorization_kind=TASK_GRANT_AUTHORIZATION_KIND,
            authorization_ref=str(used.id),
            use_ordinal=used.actions_used,
            authorization_view=_view(view.arguments),
            dispatch_constraint=BrowserDispatchConstraint(
                grant_kind="task",
                origins=(used.origin,),
                path_prefix=used.path_prefix,
                not_after=used.expires_at,
                consequence_ceiling="unknown",
                max_text_characters=256,
            ),
        )

    async def _end(
        self,
        grant: BrowserTaskGrant,
        principal: Principal,
        run: Run,
        reason: BrowserTaskGrantEndReason,
    ) -> None:
        async with self._uow_factory() as uow:
            ended, transitioned = await uow.browser_task_grants.end(
                grant.id, principal, reason=reason, now=self._now()
            )
            if transitioned:
                await uow.events.append(
                    task_grant_ended_event(ended, run_id=run.id, actor_type="runtime")
                )


def _not_covered(grant: BrowserTaskGrant, reason: str) -> StandingAuthorization:
    return StandingAuthorization(
        allowed=False,
        reason_code=_reason(reason),
        authorization_kind=TASK_GRANT_AUTHORIZATION_KIND,
        authorization_ref=str(grant.id),
    )


def _changed_pin(
    grant: BrowserTaskGrant,
    profile: BrowserProfile | None,
    agent_version: str,
    policy_version: str,
) -> BrowserTaskGrantEndReason | None:
    if (
        profile is None
        or profile.status is not BrowserProfileStatus.READY
        or profile.id != grant.profile_id
        or profile.generation != grant.profile_generation
    ):
        return BrowserTaskGrantEndReason.PROFILE_CHANGED
    if agent_version != grant.agent_version:
        return BrowserTaskGrantEndReason.AGENT_CHANGED
    if policy_version != grant.policy_version:
        return BrowserTaskGrantEndReason.POLICY_CHANGED
    return None


def _unusable(grant: BrowserTaskGrant, typed: int, now: datetime) -> str:
    """Why a guarded use found nothing to consume."""

    if grant.end_reason is not None:
        return _ENDED_REASONS.get(grant.end_reason, "ended")
    if now >= grant.expires_at:
        return "expired"
    if grant.actions_used >= grant.max_actions:
        return "exhausted"
    if grant.typed_characters + typed > TASK_GRANT_MAX_TYPED_CHARACTERS:
        return "text_budget_exhausted"
    return "unavailable"


def _view(arguments: dict[str, Any]) -> dict[str, Any]:
    """The audited view of an unreviewed action: the card's keys, redactions
    and bounds (G7)."""

    return approval_argument_view(arguments)


class CompositeStandingAuthorizer:
    """Ask the pinned standing grant first, then the task grant (D18).

    The first allow wins. A denial falls back to approval; when the task
    grant was consulted and named a grant, its denial is the most specific.
    """

    def __init__(self, authorizers: Sequence[StandingAuthorizer]) -> None:
        self._authorizers = tuple(authorizers)

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
        denials: list[StandingAuthorization] = []
        for authorizer in self._authorizers:
            try:
                result = await authorizer.authorize(
                    action=action,
                    decision=decision,
                    principal=principal,
                    run=run,
                    agent_version=agent_version,
                    action_deadline=action_deadline,
                    turn=turn,
                )
            except (AgentCoreError, OSError, ValueError):
                logger.exception("standing_authorizer_failed")
                continue
            if result.allowed:
                return result
            denials.append(result)
        specific = next(
            (
                denial
                for denial in denials
                if denial.authorization_kind == TASK_GRANT_AUTHORIZATION_KIND
                and denial.authorization_ref is not None
            ),
            None,
        )
        if specific is not None:
            return specific
        if denials:
            return denials[0]
        return StandingAuthorization(allowed=False, reason_code="standing.unavailable")
