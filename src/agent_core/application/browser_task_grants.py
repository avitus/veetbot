"""Session-bound browser task grants (ADR-0129): authorization and composition."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from agent_core.application.authorization import require_scope
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
    BrowserTaskGrantView,
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
from agent_core.domain.views import Page
from agent_core.ports.browser import BrowserProvider, browser_snapshot_in_session
from agent_core.ports.determinism import Clock
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


@dataclass(frozen=True, slots=True)
class TaskGrantResolution:
    """What the public approval service needs to allow a task (section 8.1):
    the configured scopes and the clock that stamps the grant's window."""

    scopes: tuple[BrowserTaskGrantScope, ...]
    clock: Clock


class PublicBrowserTaskGrantService:
    """List, read and revoke the principal's task grants (section 8.3)."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def list(
        self,
        principal: Principal,
        *,
        session_id: UUID | None,
        status: Literal["active", "all"],
        limit: int,
        cursor: str | None,
    ) -> Page[BrowserTaskGrantView]:
        """Newest first; an unowned session is not found, a bad cursor a
        ValueError the route answers with 400."""

        require_scope(principal, "browser.grant.read")
        if not 1 <= limit <= 200:
            raise ValueError("task grant list limit must be between 1 and 200")
        after_created_at, after_id = _decode_task_grant_cursor(cursor)
        now = self._clock.now()
        async with self._uow_factory() as uow:
            if session_id is not None:
                await uow.sessions.get(session_id, principal)
            rows = await uow.browser_task_grants.list(
                principal,
                session_id=session_id,
                active_only=status == "active",
                now=now,
                limit=limit + 1,
                after_created_at=after_created_at,
                after_id=after_id,
            )
        page = rows[:limit]
        return Page[BrowserTaskGrantView](
            items=[BrowserTaskGrantView.from_grant(row, now=now) for row in page],
            next_cursor=(
                _encode_task_grant_cursor(page[-1]) if len(rows) > limit and page else None
            ),
        )

    async def get(self, principal: Principal, grant_id: UUID) -> BrowserTaskGrantView:
        require_scope(principal, "browser.grant.read")
        async with self._uow_factory() as uow:
            grant = await uow.browser_task_grants.get(grant_id, principal)
        return BrowserTaskGrantView.from_grant(grant, now=self._clock.now())

    async def revoke(self, principal: Principal, grant_id: UUID) -> BrowserTaskGrantView:
        """End an active grant at once; an ended one returns unchanged, so a
        retry is safe and appends no second event."""

        require_scope(principal, "browser.grant.write")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            grant, transitioned = await uow.browser_task_grants.end(
                grant_id, principal, reason=BrowserTaskGrantEndReason.REVOKED, now=now
            )
            if transitioned:
                await uow.events.append(
                    task_grant_ended_event(
                        grant,
                        run_id=None,
                        actor_type="principal",
                        actor_id=principal.principal_id,
                    )
                )
        return BrowserTaskGrantView.from_grant(grant, now=now)


def _encode_task_grant_cursor(grant: BrowserTaskGrant) -> str:
    payload = json.dumps(
        {"created_at": grant.created_at.isoformat(), "id": str(grant.id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_task_grant_cursor(value: str | None) -> tuple[datetime | None, UUID | None]:
    if value is None:
        return None, None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(decoded, dict) or set(decoded) != {"created_at", "id"}:
            raise ValueError
        created_at = datetime.fromisoformat(decoded["created_at"])
        if created_at.tzinfo is None:
            raise ValueError
        return created_at, UUID(decoded["id"])
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("task grant cursor is malformed") from exc


async def sweep_expired_task_grants(
    uow_factory: UnitOfWorkFactory, clock: Clock, *, tenant_id: str, limit: int = 100
) -> int:
    """End one tenant's task grants whose window closed, in batches (section 9).

    Each ended grant gets its browser.task_grant.ended event in the same unit
    of work; a grant ends once, so a second pass appends nothing.
    """

    async with uow_factory() as uow:
        ended = await uow.browser_task_grants.end_expired(clock.now(), limit, tenant_id=tenant_id)
        for grant in ended:
            await uow.events.append(
                task_grant_ended_event(grant, run_id=None, actor_type="application")
            )
    return len(ended)


async def end_task_grants_for_profile(
    uow: Any,
    principal: Principal,
    profile_id: UUID,
    *,
    reason: BrowserTaskGrantEndReason,
    now: datetime,
) -> int:
    """End every task grant pinned to a profile, with its event, inside the
    caller's unit of work: a revoke or a new sign-in (ADR-0128 decision 10)."""

    ended = await uow.browser_task_grants.end_for_profile(
        profile_id, principal, reason=reason, now=now
    )
    for grant in ended:
        await uow.events.append(
            task_grant_ended_event(grant, run_id=None, actor_type="application")
        )
    return len(ended)
