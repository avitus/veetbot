"""Exact standing-grant authorization for revision-bound browser actions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionContext,
    BrowserGrantAuthorization,
    BrowserProfileStatus,
    BrowserProviderError,
)
from agent_core.domain.errors import AgentCoreError
from agent_core.domain.policies import (
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
)
from agent_core.domain.runs import Run
from agent_core.ports.browser import BrowserProvider, browser_action_context
from agent_core.ports.browser_grants import BrowserGrantRepository
from agent_core.ports.browser_profiles import BrowserProfileRepository
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.policies import PolicyEngine


class StandingBrowserGrantAuthorizer:
    def __init__(
        self,
        *,
        grants: BrowserGrantRepository,
        profiles: BrowserProfileRepository,
        now: Callable[[], datetime],
    ) -> None:
        self._grants = grants
        self._profiles = profiles
        self._now = now

    async def authorize(
        self,
        *,
        grant_id: UUID,
        profile_id: UUID,
        principal: Principal,
        agent_version: str,
        policy_version: str,
        purpose: str | None,
        action_deadline: datetime,
        action: BrowserAction,
        context: BrowserActionContext,
        deterministic_decision: PolicyDecisionType,
    ) -> BrowserGrantAuthorization:
        if deterministic_decision is not PolicyDecisionType.REQUIRE_APPROVAL:
            return _denied("browser.grant.policy_not_approval")
        if (
            context.consequence is not BrowserActionConsequence.ROUTINE
            or context.revision != action.expected_revision
            or context.ref != action.ref
        ):
            return _denied("browser.grant.hard_exclusion")
        try:
            grant = await self._grants.get(grant_id, principal)
            profile = await self._profiles.get(profile_id, principal)
        except (AgentCoreError, OSError):
            return _denied("browser.grant.unavailable")
        now = self._now()
        exact = (
            grant.profile_id == profile_id
            and grant.profile_generation == profile.generation
            and profile.status is BrowserProfileStatus.READY
            and grant.agent_version == agent_version
            and grant.policy_version == policy_version
            and grant.purpose == purpose
            and grant.revoked_at is None
            and grant.starts_at <= now < grant.expires_at
            and grant.expires_at >= action_deadline
            and context.origin in grant.allowed_origins
            and context.origin in profile.allowed_origins
            and action.kind in grant.action_kinds
            and (not grant.element_roles or context.role in grant.element_roles)
            and (not grant.element_names or context.name in grant.element_names)
        )
        if not exact:
            return _denied("browser.grant.mismatch")
        return BrowserGrantAuthorization(
            allowed=True,
            reason_code="browser.grant.authorized",
        )


class ConfiguredBrowserStandingAuthorizer:
    """Bind trusted profile/grant pins to the generic tool-authorization seam."""

    def __init__(
        self,
        *,
        grant_id: UUID,
        profile_id: UUID,
        purpose: str | None,
        provider: BrowserProvider,
        uow_factory: UnitOfWorkFactory,
        policy: PolicyEngine,
        now: Callable[[], datetime],
    ) -> None:
        self._grant_id = grant_id
        self._profile_id = profile_id
        self._purpose = purpose
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
    ) -> StandingAuthorization:
        denied = StandingAuthorization(
            allowed=False,
            reason_code="browser.grant.unavailable",
        )
        if (
            action.name != "browser.act"
            or action.target.kind != "browser_provider"
            or decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL
        ):
            return denied
        try:
            browser_action = BrowserAction.model_validate(action.arguments)
            context = await browser_action_context(self._provider, browser_action)
            if context is None:
                return denied
            revalidated = await self._policy.evaluate(
                action.model_copy(update={"evaluated_at": self._now()}),
                principal,
                run,
            )
            if (
                revalidated.decision is not PolicyDecisionType.REQUIRE_APPROVAL
                or revalidated.policy_version != decision.policy_version
            ):
                return StandingAuthorization(
                    allowed=False,
                    reason_code="browser.grant.policy_changed",
                )
            async with self._uow_factory() as uow:
                exact = await StandingBrowserGrantAuthorizer(
                    grants=uow.browser_grants,
                    profiles=uow.browser_profiles,
                    now=self._now,
                ).authorize(
                    grant_id=self._grant_id,
                    profile_id=self._profile_id,
                    principal=principal,
                    agent_version=agent_version,
                    policy_version=revalidated.policy_version,
                    purpose=self._purpose,
                    action_deadline=action_deadline,
                    action=browser_action,
                    context=context,
                    deterministic_decision=revalidated.decision,
                )
        except (AgentCoreError, BrowserProviderError, OSError, ValueError):
            return denied
        if not exact.allowed:
            return StandingAuthorization(
                allowed=False,
                reason_code=exact.reason_code,
            )
        return StandingAuthorization(
            allowed=True,
            reason_code=exact.reason_code,
            authorization_kind="standing_browser_grant",
            authorization_ref=str(self._grant_id),
        )


def _denied(reason_code: str) -> BrowserGrantAuthorization:
    return BrowserGrantAuthorization(allowed=False, reason_code=reason_code)
