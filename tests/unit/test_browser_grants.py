"""Standing browser grants authorize only exact routine actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from agent_core.adapters.browser.grants import InMemoryBrowserGrantRepository
from agent_core.application.browser_grants import (
    ConfiguredBrowserStandingAuthorizer,
    StandingBrowserGrantAuthorizer,
)
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionContext,
    BrowserActionKind,
    BrowserDispatchConstraint,
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserGrantAuthorization,
    BrowserLabelSource,
    BrowserObservation,
    BrowserObservationFacts,
    BrowserSnapshot,
)
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
from agent_core.domain.runs import Run
from tests.contract.support import NOW, memory_uow_factory, principal
from tests.contract.support import run as contract_run
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


# ---------------------------------------------------------------------------
# ADR-0129: the pinned standing grant sends a routine constraint (D17) and
# classifies every label source, not only the model-visible name (G2).
# ---------------------------------------------------------------------------


@dataclass
class PageProvider:
    """A provider whose cached page the authorizer reads through the port."""

    page: BrowserSnapshot
    name: str = "page-provider"

    def allows(self, url: str) -> bool:
        return url.startswith("https://example.org")

    async def navigate(self, url: str) -> BrowserObservation:
        del url
        return self.page.observation

    async def observe(self) -> BrowserObservation:
        return self.page.observation

    async def act(self, action: BrowserAction) -> BrowserObservation:
        del action
        return self.page.observation

    async def close(self) -> None:
        return

    async def action_context(self, action: BrowserAction) -> BrowserActionContext:
        element = next(item for item in self.page.observation.elements if item.ref == action.ref)
        return BrowserActionContext(
            origin="https://example.org",
            role=element.role,
            name=element.name,
            consequence=BrowserActionConsequence.ROUTINE,
            revision=self.page.observation.revision,
            ref=element.ref,
        )

    async def snapshot_in_session(self, session_id: UUID) -> BrowserSnapshot | None:
        del session_id
        return self.page


@dataclass
class RequireApproval:
    async def evaluate(self, action: ProposedAction, owner: Principal, run: Run) -> PolicyDecision:
        del action, owner, run
        return PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason_code="policy.matrix.external_write",
            explanation="External writes require approval.",
            policy_version=grant().policy_version,
        )


def continue_page(facts: BrowserElementFacts | None = None) -> BrowserSnapshot:
    return BrowserSnapshot(
        observation=BrowserObservation(
            url="https://example.org/lesson",
            revision="revision-1",
            elements=(BrowserElement(ref="revision-1:0", role="button", name="Continue"),),
        ),
        facts=BrowserObservationFacts(
            revision="revision-1",
            elements={
                "revision-1:0": facts or BrowserElementFacts(field_kind=BrowserFieldKind.NONE)
            },
        ),
    )


async def configured(page: BrowserSnapshot) -> StandingAuthorization:
    _clock, uow_factory = await memory_uow_factory()
    async with uow_factory() as uow:
        await uow.browser_profiles.create(ready_profile())
        await uow.browser_grants.create(grant())
    authorizer = ConfiguredBrowserStandingAuthorizer(
        grant_id=grant().id,
        profile_id=grant().profile_id,
        purpose="language-practice",
        provider=PageProvider(page),
        uow_factory=uow_factory,
        policy=RequireApproval(),
        now=lambda: NOW + timedelta(hours=1),
    )
    return await authorizer.authorize(
        action=ProposedAction(
            kind=ActionKind.TOOL_CALL,
            action_id=UUID(int=0xAB),
            tenant_id=principal().tenant_id,
            session_id=contract_run().session_id,
            run_id=contract_run().id,
            step_number=1,
            name="browser.act",
            summary="Run browser.act with validated arguments.",
            side_effect=SideEffectClass.EXTERNAL_WRITE,
            risk=RiskLevel.HIGH,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            arguments={"kind": "click", "expected_revision": "revision-1", "ref": "revision-1:0"},
            normalized_arguments_hash="hash",
            origin_trust=TrustLevel.USER,
            target=ExecutionTarget(kind="browser_provider", isolated=True, network_enabled=True),
            evaluated_at=NOW + timedelta(hours=1),
        ),
        decision=PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason_code="policy.matrix.external_write",
            explanation="External writes require approval.",
            policy_version=grant().policy_version,
        ),
        principal=principal(),
        run=contract_run(),
        agent_version=grant().agent_version,
        action_deadline=NOW + timedelta(hours=2),
    )


async def test_standing_grant_sends_a_routine_constraint() -> None:
    result = await configured(continue_page())

    assert result.allowed, result.reason_code
    assert result.dispatch_constraint == BrowserDispatchConstraint(
        grant_kind="standing",
        origins=grant().allowed_origins,
        not_after=grant().expires_at,
        consequence_ceiling="routine",
        max_text_characters=None,
    )


async def test_hidden_label_source_is_not_routine_for_a_standing_grant() -> None:
    """An aria-label "Continue" over visible "Pay $12.99" is a payment (G2)."""

    result = await configured(
        continue_page(
            BrowserElementFacts(
                field_kind=BrowserFieldKind.NONE,
                labels={BrowserLabelSource.VISIBLE_TEXT: "Pay $12.99"},
            )
        )
    )

    assert not result.allowed
    assert result.reason_code == "browser.grant.hard_exclusion"
