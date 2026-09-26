"""ADR-0129 section 5: the task-grant authorizer, one case per denial reason.

A denial never allows; one that names the session's grant becomes the card's
task_grant_not_covered. An allow consumes one use and carries the task
constraint, the use ordinal and the action's view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.application.browser_task_grants import (
    BrowserTaskGrantAuthorizer,
    CompositeStandingAuthorizer,
)
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserDispatchConstraint,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserSnapshot,
)
from agent_core.domain.browser_task_grants import (
    TASK_GRANT_DURATION,
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
    parse_task_grant_scopes,
    task_grant_id_for_approval,
)
from agent_core.domain.policies import (
    ActionKind,
    AuthorizationTurn,
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
from agent_core.domain.runs import Run, RunKind, RunLimits, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from tests.contract.support import NOW, memory_uow_factory, principal
from tests.unit.test_browser_act_approval_view import ORIGIN, SnapshotProvider, snapshot

SESSION = UUID("00000000-0000-0000-0000-00000000fa01")
PROFILE = UUID("00000000-0000-0000-0000-00000000fa02")
RUN = UUID("00000000-0000-0000-0000-00000000fa03")
APPROVAL = UUID("00000000-0000-0000-0000-00000000fa04")
GRANT = task_grant_id_for_approval(APPROVAL)
SCOPES = parse_task_grant_scopes(f"{ORIGIN}/lesson")
BROWSER_TURN = AuthorizationTurn(
    newest_user_trust=TrustLevel.USER, tool_names=frozenset({"browser.navigate", "browser.act"})
)
WHEN = NOW + timedelta(minutes=5)


@dataclass
class FakePolicy:
    decision: PolicyDecisionType = PolicyDecisionType.REQUIRE_APPROVAL
    version: str = "policy-v1"
    calls: list[ProposedAction] = field(default_factory=list)

    async def evaluate(self, action: ProposedAction, owner: Principal, run: Run) -> PolicyDecision:
        del owner, run
        self.calls.append(action)
        return PolicyDecision(
            decision=self.decision,
            reason_code="policy.matrix.external_write",
            explanation="External writes require approval.",
            policy_version=self.version,
        )


def owner() -> Principal:
    return principal()


def run(**update: Any) -> Run:
    return Run(
        id=RUN,
        session_id=SESSION,
        tenant_id=owner().tenant_id,
        agent_id=UUID(int=0xA6),
        agent_version="agent-v1",
        status=RunStatus.RUNNING,
        limits=RunLimits(),
        created_at=NOW,
        updated_at=NOW,
    ).model_copy(update=update)


def proposed(arguments: dict[str, Any] | None = None, name: str = "browser.act") -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=0xAC),
        tenant_id=owner().tenant_id,
        session_id=SESSION,
        run_id=RUN,
        step_number=3,
        name=name,
        summary="Run browser.act with validated arguments.",
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        arguments=arguments
        or {"kind": "click", "expected_revision": "revision-7", "ref": "revision-7:0"},
        normalized_arguments_hash="hash",
        origin_trust=TrustLevel.USER,
        target=ExecutionTarget(kind="browser_provider", isolated=True, network_enabled=True),
        evaluated_at=WHEN,
    )


def decision() -> PolicyDecision:
    return PolicyDecision(
        decision=PolicyDecisionType.REQUIRE_APPROVAL,
        reason_code="policy.matrix.external_write",
        explanation="External writes require approval.",
        policy_version="policy-v1",
    )


@dataclass
class Harness:
    authorizer: BrowserTaskGrantAuthorizer
    uow_factory: Any
    provider: SnapshotProvider
    policy: FakePolicy
    clock: list[datetime]

    async def authorize(
        self,
        action: ProposedAction | None = None,
        *,
        run_update: dict[str, Any] | None = None,
        turn: AuthorizationTurn | None = BROWSER_TURN,
        agent_version: str = "agent-v1",
    ) -> StandingAuthorization:
        return await self.authorizer.authorize(
            action=action or proposed(),
            decision=decision(),
            principal=owner(),
            run=run(**(run_update or {})),
            agent_version=agent_version,
            action_deadline=self.clock[0] + timedelta(seconds=30),
            turn=turn,
        )

    async def grant(self) -> BrowserTaskGrant:
        async with self.uow_factory() as uow:
            stored: BrowserTaskGrant = await uow.browser_task_grants.get(GRANT, owner())
            return stored

    async def ended_events(self) -> list[dict[str, Any]]:
        async with self.uow_factory() as uow:
            events = await uow.events.list_after(SESSION, 0, owner())
        return [event.payload for event in events if event.event_type == "browser.task_grant.ended"]


async def harness(
    *,
    page: BrowserSnapshot | None = None,
    metadata: dict[str, Any] | None = None,
    profile_update: dict[str, Any] | None = None,
    grant_update: dict[str, Any] | None = None,
    scopes: Any = SCOPES,
    with_grant: bool = True,
) -> Harness:
    _clock, uow_factory = await memory_uow_factory()
    async with uow_factory() as uow:
        await uow.sessions.create(
            Session(
                id=SESSION,
                tenant_id=owner().tenant_id,
                principal_id=owner().principal_id,
                agent_id=UUID(int=0xA6),
                agent_version="agent-v1",
                status=SessionStatus.ACTIVE,
                metadata={"browser_profile_id": str(PROFILE)} if metadata is None else metadata,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.browser_profiles.create(
            BrowserProfile(
                id=PROFILE,
                tenant_id=owner().tenant_id,
                principal_id=owner().principal_id,
                provider_name="hosted-isolated",
                provider_ref="opaque-provider-reference",
                allowed_origins=(ORIGIN,),
                status=BrowserProfileStatus.READY,
                generation=3,
                encryption_key_version="key-v1",
                created_at=NOW,
                updated_at=NOW,
            ).model_copy(update=profile_update or {})
        )
        if with_grant:
            await uow.browser_task_grants.create(
                BrowserTaskGrant(
                    id=GRANT,
                    tenant_id=owner().tenant_id,
                    principal_id=owner().principal_id,
                    session_id=SESSION,
                    profile_id=PROFILE,
                    profile_generation=3,
                    agent_version="agent-v1",
                    policy_version="policy-v1",
                    origin=ORIGIN,
                    path_prefix="/lesson",
                    max_actions=200,
                    actions_used=0,
                    typed_characters=0,
                    approval_id=APPROVAL,
                    approved_by=owner().principal_id,
                    created_at=NOW,
                    expires_at=NOW + TASK_GRANT_DURATION,
                ).model_copy(update=grant_update or {})
            )
    provider = SnapshotProvider(allowed_origins=(ORIGIN,))
    provider.snapshots[SESSION] = page or snapshot()
    policy = FakePolicy()
    clock = [WHEN]
    return Harness(
        authorizer=BrowserTaskGrantAuthorizer(
            provider=provider,
            uow_factory=uow_factory,
            policy=policy,
            scopes=scopes,
            now=lambda: clock[0],
        ),
        uow_factory=uow_factory,
        provider=provider,
        policy=policy,
        clock=clock,
    )


def refused(result: StandingAuthorization, reason: str) -> None:
    assert not result.allowed
    assert result.reason_code == f"browser.task_grant.{reason}"
    assert (result.authorization_kind, result.authorization_ref) == (
        "browser_task_grant",
        str(GRANT),
    )


async def test_a_covered_click_is_allowed_with_the_task_constraint_and_its_view() -> None:
    subject = await harness()

    result = await subject.authorize()
    stored = await subject.grant()

    assert result.allowed
    assert result.reason_code == "browser.task_grant.authorized"
    assert (result.authorization_kind, result.authorization_ref) == (
        "browser_task_grant",
        str(GRANT),
    )
    assert result.use_ordinal == 1
    assert result.dispatch_constraint == BrowserDispatchConstraint(
        grant_kind="task",
        origins=(ORIGIN,),
        path_prefix="/lesson",
        not_after=NOW + TASK_GRANT_DURATION,
        consequence_ceiling="unknown",
        max_text_characters=256,
    )
    assert result.authorization_view is not None
    assert result.authorization_view["element_name"] == "el gato"
    assert "ref" not in result.authorization_view
    assert (stored.actions_used, stored.last_used_at) == (1, WHEN)


async def test_typed_text_counts_against_the_grant() -> None:
    subject = await harness(
        page=snapshot(role="textbox", facts=BrowserElementFacts(field_kind=BrowserFieldKind.TEXT))
    )

    result = await subject.authorize(
        proposed(
            {
                "kind": "type",
                "expected_revision": "revision-7",
                "ref": "revision-7:0",
                "value": "el gato",
            }
        )
    )

    assert result.allowed
    assert (await subject.grant()).typed_characters == len("el gato")


async def test_an_action_that_is_not_browser_act_is_not_a_task_grant_question() -> None:
    subject = await harness()

    result = await subject.authorize(proposed(name="web.fetch"))

    assert not result.allowed
    assert result.authorization_kind is None


async def test_no_active_grant_is_a_denial_with_no_record() -> None:
    subject = await harness(with_grant=False)

    result = await subject.authorize()

    assert not result.allowed
    assert result.authorization_kind is None and result.authorization_ref is None


@pytest.mark.parametrize(
    ("run_update", "turn", "metadata"),
    [
        ({"parent_run_id": UUID(int=0xC1)}, BROWSER_TURN, None),
        ({"kind": RunKind.DELEGATED}, BROWSER_TURN, None),
        ({}, AuthorizationTurn(newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED), None),
        ({}, None, None),
        (
            {},
            BROWSER_TURN,
            {"browser_profile_id": str(PROFILE), "schedule_id": str(UUID(int=0x5C))},
        ),
    ],
)
async def test_an_ineligible_run_is_refused(
    run_update: dict[str, Any], turn: AuthorizationTurn | None, metadata: dict[str, Any] | None
) -> None:
    subject = await harness(metadata=metadata)

    refused(await subject.authorize(run_update=run_update, turn=turn), "run_not_eligible")
    assert (await subject.grant()).actions_used == 0


async def test_a_removed_scope_ends_the_grant() -> None:
    subject = await harness(scopes=parse_task_grant_scopes(f"{ORIGIN}/practice"))

    refused(await subject.authorize(), "scope_removed")
    assert (await subject.grant()).end_reason is BrowserTaskGrantEndReason.SCOPE_REMOVED
    assert [event["reason"] for event in await subject.ended_events()] == ["scope_removed"]


async def test_a_turn_that_called_another_tool_is_refused() -> None:
    subject = await harness()
    turn = AuthorizationTurn(
        newest_user_trust=TrustLevel.USER, tool_names=frozenset({"browser.act", "email.search"})
    )

    refused(await subject.authorize(turn=turn), "turn_not_browser_only")


async def test_a_stale_observation_is_unavailable() -> None:
    subject = await harness()
    subject.provider.snapshots.clear()

    refused(await subject.authorize(), "unavailable")


async def test_a_revalidation_that_changes_policy_is_refused() -> None:
    subject = await harness()
    subject.policy.version = "policy-v2"

    refused(await subject.authorize(), "policy_changed")


@pytest.mark.parametrize(
    ("profile_update", "agent_version", "grant_update", "reason"),
    [
        ({"generation": 4}, "agent-v1", {}, "profile_changed"),
        ({"status": BrowserProfileStatus.NEEDS_USER}, "agent-v1", {}, "profile_changed"),
        ({}, "agent-v2", {}, "agent_changed"),
        ({}, "agent-v1", {"policy_version": "policy-v0"}, "policy_changed"),
    ],
)
async def test_a_changed_pin_ends_the_grant(
    profile_update: dict[str, Any], agent_version: str, grant_update: dict[str, Any], reason: str
) -> None:
    subject = await harness(profile_update=profile_update, grant_update=grant_update)

    refused(await subject.authorize(agent_version=agent_version), reason)
    assert (await subject.grant()).end_reason == BrowserTaskGrantEndReason(reason)
    assert [event["reason"] for event in await subject.ended_events()] == [reason]


async def test_an_excluded_action_names_its_consequence() -> None:
    subject = await harness(page=snapshot(name="Delete my progress"))

    refused(await subject.authorize(), "excluded.destructive")
    assert (await subject.grant()).actions_used == 0


async def test_an_expired_grant_is_refused() -> None:
    subject = await harness()
    subject.clock[0] = NOW + TASK_GRANT_DURATION

    result = await subject.authorize()

    assert not result.allowed
    assert result.reason_code in {"browser.task_grant.expired", "browser.task_grant.none"}


async def test_the_use_that_reaches_the_cap_ends_the_grant_exhausted() -> None:
    subject = await harness(grant_update={"max_actions": 2})

    first = await subject.authorize()
    last = await subject.authorize()
    after = await subject.authorize()

    assert (first.use_ordinal, last.use_ordinal) == (1, 2)
    assert (await subject.grant()).end_reason is BrowserTaskGrantEndReason.EXHAUSTED
    assert [event["reason"] for event in await subject.ended_events()] == ["exhausted"]
    assert not after.allowed


async def test_a_revoked_grant_is_refused() -> None:
    subject = await harness()
    async with subject.uow_factory() as uow:
        await uow.browser_task_grants.end(
            GRANT, owner(), reason=BrowserTaskGrantEndReason.REVOKED, now=WHEN
        )

    assert not (await subject.authorize()).allowed


async def test_typed_text_past_the_budget_is_refused() -> None:
    subject = await harness(
        page=snapshot(role="textbox", facts=BrowserElementFacts(field_kind=BrowserFieldKind.TEXT)),
        grant_update={"typed_characters": 4090},
    )

    refused(
        await subject.authorize(
            proposed(
                {
                    "kind": "type",
                    "expected_revision": "revision-7",
                    "ref": "revision-7:0",
                    "value": "el gato",
                }
            )
        ),
        "text_budget_exhausted",
    )


@dataclass
class Fixed:
    result: StandingAuthorization
    calls: int = 0

    async def authorize(self, **kwargs: Any) -> StandingAuthorization:
        del kwargs
        self.calls += 1
        return self.result


async def test_the_composite_asks_the_standing_grant_first_and_keeps_the_specific_denial() -> None:
    standing_allow = StandingAuthorization(
        allowed=True,
        reason_code="browser.grant.authorized",
        authorization_kind="standing_browser_grant",
        authorization_ref="grant",
    )
    standing_deny = StandingAuthorization(allowed=False, reason_code="browser.grant.mismatch")
    task_deny = StandingAuthorization(
        allowed=False,
        reason_code="browser.task_grant.excluded.payment",
        authorization_kind="browser_task_grant",
        authorization_ref=str(GRANT),
    )
    no_grant = StandingAuthorization(allowed=False, reason_code="browser.task_grant.none")
    arguments: dict[str, Any] = {
        "action": proposed(),
        "decision": decision(),
        "principal": owner(),
        "run": run(),
        "agent_version": "agent-v1",
        "action_deadline": WHEN,
        "turn": BROWSER_TURN,
    }

    first, second = Fixed(standing_allow), Fixed(task_deny)
    allowed = await CompositeStandingAuthorizer([first, second]).authorize(**arguments)
    specific = await CompositeStandingAuthorizer(
        [Fixed(standing_deny), Fixed(task_deny)]
    ).authorize(**arguments)
    general = await CompositeStandingAuthorizer([Fixed(standing_deny), Fixed(no_grant)]).authorize(
        **arguments
    )

    assert allowed == standing_allow and second.calls == 0
    assert specific == task_deny
    assert general == standing_deny
