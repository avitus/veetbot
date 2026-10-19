"""Explicit owner-profile permission for ordinary owner browser interactions."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.argument_views import approval_argument_view
from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserProfileStatus,
    browser_origin,
)
from agent_core.domain.browser_act_views import (
    describe_browser_action,
    element_labels,
    option_texts,
    turn_is_browser_only,
)
from agent_core.domain.browser_classification import task_grant_coverage
from agent_core.domain.errors import NotFoundError
from agent_core.domain.policies import (
    ActionKind,
    AuthorizationTurn,
    IdempotencyClass,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind
from agent_core.domain.sessions import (
    SESSION_BROWSER_PROFILE_METADATA_KEY,
    SESSION_SCHEDULE_ID_METADATA_KEY,
)
from agent_core.ports.browser import BrowserProvider, browser_snapshot_in_session
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.policies import PolicyEngine


class BrowserResearchAuthorizer:
    """Composed only when research is enabled; a refusal uses ordinary approval."""

    def __init__(
        self,
        *,
        provider: BrowserProvider,
        uow_factory: UnitOfWorkFactory,
        policy: PolicyEngine,
        now: Callable[[], datetime],
    ) -> None:
        """Bind trusted profile, policy, snapshot and clock dependencies for each authorization."""
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
        """Authorize an ordinary owner-origin action only with current profile and page evidence."""
        refused = StandingAuthorization(allowed=False, reason_code="browser.research.not_covered")
        if not (
            action.kind is ActionKind.TOOL_CALL
            and action.name == "browser.act"
            and action.target.kind == "browser_provider"
            and action.target.isolated
            and action.target.network_enabled
            and action.side_effect is SideEffectClass.EXTERNAL_WRITE
            and action.risk is RiskLevel.HIGH
            and action.idempotency is IdempotencyClass.NON_IDEMPOTENT
            and decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
            and run.kind is RunKind.INTERACTIVE
            and run.parent_run_id is None
            and run.tenant_id == principal.tenant_id
            and run.agent_version == agent_version
            and "browser.profile.read" in principal.scopes
            and turn is not None
            and turn.newest_user_trust is TrustLevel.USER
            # ADR-0129 G5: a turn that called any non-browser tool (mail, memory,
            # search) could type what it read, so it keeps ordinary approval.
            and turn_is_browser_only(turn)
            and self._now() < action_deadline
        ):
            return refused
        try:
            browser_action = BrowserAction.model_validate(action.arguments)
            async with self._uow_factory() as uow:
                session = await uow.sessions.get(run.session_id, principal)
                if session.principal_id != principal.principal_id:
                    return refused
                selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
                if not isinstance(selected, str):
                    return refused
                profile = await uow.browser_profiles.get(UUID(selected), principal)
                seed = await uow.events.latest_before(
                    session.id, run.seed_event_sequence + 1, "user.message.created", principal
                )
                if not (
                    seed is not None
                    and seed.run_id == run.id
                    and seed.sequence == run.seed_event_sequence
                    and seed.actor_id == principal.principal_id
                    and (
                        seed.actor_type == "principal"
                        or (
                            seed.actor_type == "scheduler"
                            and SESSION_SCHEDULE_ID_METADATA_KEY in session.metadata
                        )
                    )
                    and profile.status is BrowserProfileStatus.READY
                    and profile.principal_id == principal.principal_id
                ):
                    return refused
        except (ValueError, NotFoundError):
            return refused
        view = describe_browser_action(
            browser_action, await browser_snapshot_in_session(self._provider, run.session_id)
        )
        if not view.described or view.observation is None or view.element is None:
            return refused
        origin = browser_origin(view.observation.url)
        if origin not in profile.allowed_origins:
            return refused
        covered = task_grant_coverage(
            action=browser_action,
            page_url=view.observation.url,
            role=view.element.role,
            labels=element_labels(view.element, view.facts),
            facts=view.facts,
            option_texts=option_texts(browser_action),
            origin=origin,
            path_prefix=None,
            disabled=view.element.disabled,
        )
        if not covered.covered:
            return refused
        current = await self._policy.evaluate(
            action.model_copy(update={"evaluated_at": self._now()}), principal, run
        )
        if (
            current.decision is not PolicyDecisionType.REQUIRE_APPROVAL
            or current.policy_version != decision.policy_version
        ):
            return refused
        return StandingAuthorization(
            allowed=True,
            reason_code="browser.research.authorized",
            authorization_kind="browser_research",
            authorization_ref=f"{profile.id}:{profile.generation}:{seed.sequence}",
            authorization_view=approval_argument_view(view.arguments),
            dispatch_constraint=BrowserDispatchConstraint(
                grant_kind="research",
                origins=(origin,),
                not_after=action_deadline,
                consequence_ceiling="unknown",
                max_text_characters=256,
            ),
        )
