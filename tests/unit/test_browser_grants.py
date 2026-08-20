"""Standing browser grants authorize only exact routine actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from agent_core.adapters.browser.grants import InMemoryBrowserGrantRepository
from agent_core.application.browser_grants import StandingBrowserGrantAuthorizer
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionContext,
    BrowserActionKind,
    BrowserGrantAuthorization,
)
from agent_core.domain.policies import PolicyDecisionType
from tests.contract.support import NOW, principal
from tests.contract.test_browser_grant_repository_contract import grant, ready_profile


@dataclass
class ProfileRepository:
    profile: object

    async def get(self, profile_id, owner):  # type: ignore[no-untyped-def]
        del profile_id, owner
        return self.profile


async def authorizer(
    *, now_offset: timedelta = timedelta(hours=1)
) -> tuple[StandingBrowserGrantAuthorizer, InMemoryBrowserGrantRepository]:
    grants = InMemoryBrowserGrantRepository()
    await grants.create(grant())
    return (
        StandingBrowserGrantAuthorizer(
            grants=grants,
            profiles=ProfileRepository(ready_profile()),  # type: ignore[arg-type]
            now=lambda: NOW + now_offset,
        ),
        grants,
    )


async def authorize(
    service: StandingBrowserGrantAuthorizer,
    *,
    consequence: BrowserActionConsequence = BrowserActionConsequence.ROUTINE,
    policy_version: str | None = None,
    decision: PolicyDecisionType = PolicyDecisionType.REQUIRE_APPROVAL,
) -> BrowserGrantAuthorization:
    return await service.authorize(
        grant_id=grant().id,
        profile_id=grant().profile_id,
        principal=principal(),
        agent_version=grant().agent_version,
        policy_version=policy_version or grant().policy_version,
        purpose="language-practice",
        action_deadline=NOW + timedelta(hours=2),
        action=BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision="revision-1",
            ref="revision-1:0",
        ),
        context=BrowserActionContext(
            origin="https://example.org",
            role="button",
            name="Continue",
            consequence=consequence,
            revision="revision-1",
            ref="revision-1:0",
        ),
        deterministic_decision=decision,
    )


async def test_exact_routine_action_can_replace_one_approval() -> None:
    service, _grants = await authorizer()
    result = await authorize(service)

    assert result.allowed is True
    assert result.reason_code == "browser.grant.authorized"


async def test_expired_revoked_mismatched_or_excluded_grant_fails_closed() -> None:
    service, grants = await authorizer()
    expired_service, _expired_grants = await authorizer(now_offset=timedelta(days=8))
    excluded = await authorize(service, consequence=BrowserActionConsequence.PURCHASE)
    expired = await authorize(expired_service)
    await grants.revoke(grant().id, principal(), revoked_at=NOW + timedelta(minutes=30))
    revoked = await authorize(service)
    wrong_policy = await authorize(service, policy_version="other-policy")

    assert excluded.allowed is False
    assert excluded.reason_code == "browser.grant.hard_exclusion"
    assert expired.allowed is False
    assert expired.reason_code == "browser.grant.mismatch"
    assert revoked.allowed is False
    assert revoked.reason_code == "browser.grant.mismatch"
    assert wrong_policy.allowed is False
    assert wrong_policy.reason_code == "browser.grant.mismatch"


async def test_policy_allow_or_deny_is_never_overridden() -> None:
    service, _grants = await authorizer()
    denied = await authorize(service, decision=PolicyDecisionType.DENY)
    already_allowed = await authorize(service, decision=PolicyDecisionType.ALLOW)

    assert denied.allowed is False
    assert denied.reason_code == "browser.grant.policy_not_approval"
    assert already_allowed.allowed is False
    assert already_allowed.reason_code == "browser.grant.policy_not_approval"
