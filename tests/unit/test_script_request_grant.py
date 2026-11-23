"""ADR-0342 decision 11: one approval for the scripts of a request.

A script written after the turn read outside content asks, and so did every
later script of that request. Its card now offers "Allow for this request";
resolving it with ``approve_for_request`` records the grant, which lets the
later scripts of the same run start without a card. The grant covers starting
a script and nothing else: each call a script makes is still decided by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest

from agent_core.application.public_services import PublicApprovalService
from agent_core.application.script_request_grants import (
    ScriptRequestGrantAuthorizer,
    ScriptRequestGrantResolution,
    script_request_grant_offer,
)
from agent_core.domain.approvals import (
    ApprovalRequest,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.browser_request_grants import RequestGrantEcho
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import NewEvent
from agent_core.domain.execution import SANDBOX_RUN_CODE_TOOL_NAME
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
from agent_core.domain.runs import Run, RunKind, RunStatus
from agent_core.domain.script_request_grants import (
    SCRIPT_REQUEST_GRANT_AUTHORIZATION_KIND,
    SCRIPT_REQUEST_GRANT_CREATED_EVENT,
    SCRIPT_REQUEST_GRANT_SUMMARY,
    ScriptRequestGrantOffer,
)
from agent_core.domain.sessions import Session, SessionStatus
from tests.contract.support import NOW, memory_uow_factory, principal
from tests.unit.test_job_request_grant import (
    APPROVAL,
    OTHER_RUN,
    OWNER_TURN,
    RUN,
    SESSION,
    SLUG,
    WHEN,
    Dispatcher,
    _Clock,
    a_run,
    owner,
)

SCRIPT: Final = SANDBOX_RUN_CODE_TOOL_NAME
ECHO = RequestGrantEcho(tool_name=SCRIPT)


def decision(
    version: str = "policy-v1",
    *,
    untrusted: bool = True,
    kind: PolicyDecisionType = PolicyDecisionType.REQUIRE_APPROVAL,
) -> PolicyDecision:
    return PolicyDecision(
        decision=kind,
        reason_code="policy.matrix.code_execution",
        explanation="The default profile evaluated code_execution.",
        policy_version=version,
        untrusted_escalation=untrusted,
    )


def offer() -> ScriptRequestGrantOffer:
    return ScriptRequestGrantOffer(summary=SCRIPT_REQUEST_GRANT_SUMMARY)


def approval(**update: Any) -> ApprovalRequest:
    return ApprovalRequest(
        id=APPROVAL,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        session_id=SESSION,
        run_id=RUN,
        action_kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=0x1C1),
        tool_invocation_id=UUID(int=0x1C1),
        status=ApprovalStatus.PENDING,
        action_summary="Run code with sandbox.run_code",
        tool_name=SCRIPT,
        arguments={"code": "print(6 * 7)"},
        normalized_arguments_hash="hash",
        required_scopes=set(),
        agent_version="agent-v1",
        risk=RiskLevel.MEDIUM,
        policy_reason="policy.matrix.code_execution",
        policy_decision=decision(),
        policy_version="policy-v1",
        expires_at=NOW + timedelta(hours=4),
        created_at=NOW,
        script_request_grant_offer=offer(),
    ).model_copy(update=update)


def proposed(name: str = SCRIPT) -> ProposedAction:
    arguments = {"code": "print('a later script')"}
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=0xAC),
        tenant_id=principal().tenant_id,
        session_id=SESSION,
        run_id=RUN,
        step_number=3,
        name=name,
        version="1.0.0",
        summary=f"Use {name}.",
        side_effect=SideEffectClass.CODE_EXECUTION,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        arguments=arguments,
        normalized_arguments_hash="hash",
        argument_trust=dict.fromkeys(arguments, TrustLevel.EXTERNAL_UNTRUSTED),
        origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        target=ExecutionTarget(kind="sandbox", isolated=True, network_enabled=False),
        evaluated_at=WHEN,
    )


@dataclass
class World:
    uow_factory: Any
    clock: list[datetime]
    dispatcher: Dispatcher

    def service(self, *, composed: bool = True) -> PublicApprovalService:
        async def resume_waiting_run(uow: Any, run: Run) -> Run:
            del uow
            return run

        return PublicApprovalService(
            uow_factory=self.uow_factory,
            dispatcher=self.dispatcher,
            resume_waiting_run=resume_waiting_run,
            self_approval_enabled=True,
            script_request_grants=(
                ScriptRequestGrantResolution(clock=_Clock(self.clock)) if composed else None
            ),
        )

    async def authorize(
        self,
        name: str = SCRIPT,
        *,
        run_update: dict[str, Any] | None = None,
        turn: AuthorizationTurn | None = OWNER_TURN,
        made: PolicyDecision | None = None,
        agent_version: str = "agent-v1",
    ) -> StandingAuthorization:
        return await ScriptRequestGrantAuthorizer(
            uow_factory=self.uow_factory, now=lambda: self.clock[0]
        ).authorize(
            action=proposed(name),
            decision=made or decision(),
            principal=principal(),
            run=a_run(status=RunStatus.RUNNING, **(run_update or {})),
            agent_version=agent_version,
            action_deadline=self.clock[0] + timedelta(seconds=30),
            turn=turn,
        )

    async def offer_for(
        self,
        name: str = SCRIPT,
        *,
        run_update: dict[str, Any] | None = None,
        turn: AuthorizationTurn | None = OWNER_TURN,
        made: PolicyDecision | None = None,
    ) -> ScriptRequestGrantOffer | None:
        async with self.uow_factory() as uow:
            return await script_request_grant_offer(
                uow,
                tool_name=name,
                decision=made or decision(),
                run=a_run(**(run_update or {})),
                principal=principal(),
                turn=turn,
            )

    async def events(self, event_type: str) -> list[dict[str, Any]]:
        async with self.uow_factory() as uow:
            events = await uow.events.list_after(SESSION, 0, principal())
        return [event.payload for event in events if event.event_type == event_type]

    async def append(self, event_type: str, payload: dict[str, Any], run_id: UUID = RUN) -> None:
        async with self.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=SESSION,
                    run_id=run_id,
                    event_type=event_type,
                    actor_type="application",
                    payload=payload,
                )
            )

    async def stored(self) -> ApprovalRequest:
        async with self.uow_factory() as uow:
            found: ApprovalRequest = await uow.approvals.get(APPROVAL, principal())
            return found


async def world(
    *,
    granted: bool = False,
    metadata: dict[str, Any] | None = None,
    approval_update: dict[str, Any] | None = None,
) -> World:
    _fixed, uow_factory = await memory_uow_factory()
    async with uow_factory() as uow:
        await uow.sessions.create(
            Session(
                id=SESSION,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                agent_id=UUID(int=0xA6),
                agent_version="agent-v1",
                status=SessionStatus.ACTIVE,
                metadata=metadata or {},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.runs.create(a_run())
        stored = approval(**(approval_update or {}))
        if granted:
            stored = stored.model_copy(
                update={
                    "status": ApprovalStatus.APPROVED,
                    "resolution": ApprovalResolutionType.APPROVE_FOR_REQUEST,
                    "resolved_at": NOW,
                    "resolved_by": principal().principal_id,
                }
            )
        await uow.approvals.create(stored)
    subject = World(uow_factory=uow_factory, clock=[WHEN], dispatcher=Dispatcher([]))
    if granted:
        await subject.append(SCRIPT_REQUEST_GRANT_CREATED_EVENT, {"approval_id": str(APPROVAL)})
    return subject


# The offer.


async def test_a_script_card_raised_by_outside_content_offers_the_request() -> None:
    subject = await world()

    made = await subject.offer_for()

    assert made == offer()
    assert (made.tool_name, made.duration_seconds) == (SCRIPT, 7200)
    # The card says what stays checked: the calls a script makes.
    assert "still" in made.summary


@pytest.mark.parametrize(
    "case",
    [
        "other_tool",
        "policy_itself_asks",
        "refresh_session",
        "schedule_session",
        "job_run",
        "child_run",
        "foreign_turn",
        "no_turn",
    ],
)
async def test_no_offer_outside_an_owner_request_or_for_another_reason_to_ask(case: str) -> None:
    markers: dict[str, dict[str, Any]] = {
        "refresh_session": {"cockpit_refresh": True},
        "schedule_session": {"schedule_id": str(UUID(int=7))},
    }
    subject = await world(metadata=markers.get(case))
    name = "sandbox.run_command" if case == "other_tool" else SCRIPT
    # A profile that asks before any code execution is not what the grant covers.
    made = decision(untrusted=False) if case == "policy_itself_asks" else None
    run_update: dict[str, Any] = {}
    turn: AuthorizationTurn | None = OWNER_TURN
    if case == "job_run":
        run_update = {"kind": RunKind.JOB}
    elif case == "child_run":
        run_update = {"parent_run_id": OTHER_RUN}
    elif case == "foreign_turn":
        turn = AuthorizationTurn(newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED)
    elif case == "no_turn":
        turn = None

    assert await subject.offer_for(name, run_update=run_update, turn=turn, made=made) is None


# Resolution.


async def test_approve_for_request_approves_once_and_records_the_script_grant() -> None:
    subject = await world()

    view = await subject.service().resolve(
        owner("approval.resolve"),
        APPROVAL,
        ApprovalResolutionType.APPROVE_FOR_REQUEST,
        None,
        request_grant=ECHO,
    )

    assert view.status == "APPROVED"
    assert view.decision is ApprovalResolutionType.APPROVE_FOR_REQUEST
    assert view.script_request_grant_offer == offer()
    created = await subject.events(SCRIPT_REQUEST_GRANT_CREATED_EVENT)
    assert [item["approval_id"] for item in created] == [str(APPROVAL)]
    stored = await subject.stored()
    assert stored.resolved_at is not None
    assert created[0]["expires_at"] == (stored.resolved_at + timedelta(hours=2)).isoformat()
    assert subject.dispatcher.resumed == [RUN]
    # A retry of the same allow returns the stored view.
    again = await subject.service().resolve(
        owner("approval.resolve"),
        APPROVAL,
        ApprovalResolutionType.APPROVE_FOR_REQUEST,
        None,
        request_grant=ECHO,
    )
    assert again.decision is ApprovalResolutionType.APPROVE_FOR_REQUEST
    assert len(await subject.events(SCRIPT_REQUEST_GRANT_CREATED_EVENT)) == 1


@pytest.mark.parametrize(
    "case",
    ["no_offer", "not_composed", "profile_echo", "job_echo", "other_tool", "ineligible_session"],
)
async def test_a_stale_or_different_script_offer_leaves_the_card_pending(case: str) -> None:
    update: dict[str, Any] | None = None
    if case == "no_offer":
        update = {"script_request_grant_offer": None}
    elif case == "other_tool":
        update = {"tool_name": "sandbox.run_command"}
    subject = await world(
        approval_update=update,
        metadata={"cockpit_refresh": True} if case == "ineligible_session" else None,
    )
    echo = (
        RequestGrantEcho(profile_id=UUID(int=5))
        if case == "profile_echo"
        else RequestGrantEcho(job_slug=SLUG)
        if case == "job_echo"
        else ECHO
    )

    with pytest.raises(ConflictError) as raised:
        await subject.service(composed=case != "not_composed").resolve(
            owner("approval.resolve", "jobs.run", "browser.grant.write"),
            APPROVAL,
            ApprovalResolutionType.APPROVE_FOR_REQUEST,
            None,
            request_grant=echo,
        )

    assert raised.value.reason == "request_grant_unavailable"
    assert (await subject.stored()).status is ApprovalStatus.PENDING
    assert await subject.events(SCRIPT_REQUEST_GRANT_CREATED_EVENT) == []


async def test_a_script_card_resolved_another_way_refuses_a_later_allow() -> None:
    subject = await world(
        approval_update={
            "status": ApprovalStatus.APPROVED,
            "resolution": ApprovalResolutionType.APPROVE_ONCE,
            "resolved_at": NOW,
            "resolved_by": principal().principal_id,
        }
    )

    with pytest.raises(ConflictError) as raised:
        await subject.service().resolve(
            owner("approval.resolve"),
            APPROVAL,
            ApprovalResolutionType.APPROVE_FOR_REQUEST,
            None,
            request_grant=ECHO,
        )

    assert raised.value.reason == "approval_already_resolved"
    assert await subject.events(SCRIPT_REQUEST_GRANT_CREATED_EVENT) == []


def test_the_echo_names_exactly_one_offer() -> None:
    assert ECHO.tool_name == SCRIPT
    with pytest.raises(ValueError):
        RequestGrantEcho()
    with pytest.raises(ValueError):
        RequestGrantEcho(job_slug=SLUG, tool_name=SCRIPT)
    with pytest.raises(ValueError):
        RequestGrantEcho(profile_id=UUID(int=5), tool_name=SCRIPT)
    with pytest.raises(ValueError):
        RequestGrantEcho.model_validate({"tool_name": "sandbox.run_command"})


# Coverage.


async def test_the_grant_covers_the_later_scripts_of_the_request() -> None:
    subject = await world(granted=True)

    result = await subject.authorize()

    assert result.allowed, result.reason_code
    assert result.authorization_kind == SCRIPT_REQUEST_GRANT_AUTHORIZATION_KIND
    assert result.authorization_ref == str(APPROVAL)


@pytest.mark.parametrize("case", ["other_tool", "policy_itself_asks", "already_allowed"])
async def test_the_grant_covers_a_script_and_only_the_card_outside_content_raises(
    case: str,
) -> None:
    subject = await world(granted=True)
    made = (
        decision(untrusted=False)
        if case == "policy_itself_asks"
        else decision(untrusted=False, kind=PolicyDecisionType.ALLOW)
        if case == "already_allowed"
        else None
    )

    result = await subject.authorize(
        "sandbox.run_command" if case == "other_tool" else SCRIPT, made=made
    )

    assert not result.allowed
    assert result.reason_code == "script.request_grant.none"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("expired", "script.request_grant.expired"),
        ("other_run", "script.request_grant.none"),
        ("follow_up", "script.request_grant.none"),
        ("agent_changed", "script.request_grant.agent_changed"),
        ("policy_changed", "script.request_grant.policy_changed"),
        ("foreign_turn", "script.request_grant.run_not_eligible"),
        ("child_run", "script.request_grant.run_not_eligible"),
        ("none", "script.request_grant.none"),
    ],
)
async def test_the_grant_ends_with_its_request(case: str, reason: str) -> None:
    subject = await world(granted=case != "none")
    run_update: dict[str, Any] = {}
    turn: AuthorizationTurn | None = OWNER_TURN
    if case == "expired":
        subject.clock[0] = NOW + timedelta(hours=2)
    elif case == "other_run":
        run_update = {"id": OTHER_RUN}
    elif case == "child_run":
        run_update = {"parent_run_id": OTHER_RUN}
    elif case == "follow_up":
        await subject.append(
            "user.message.created", {"pending_input_id": str(UUID(int=9)), "text": "also"}
        )
    elif case == "foreign_turn":
        turn = AuthorizationTurn(newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED)

    result = await subject.authorize(
        run_update=run_update,
        turn=turn,
        made=decision("policy-v2") if case == "policy_changed" else None,
        agent_version="agent-v2" if case == "agent_changed" else "agent-v1",
    )

    assert not result.allowed
    assert result.reason_code == reason


async def test_a_job_request_grant_does_not_cover_a_script() -> None:
    subject = await world(
        granted=True,
        approval_update={"script_request_grant_offer": None, "tool_name": "job.manage"},
    )

    result = await subject.authorize()

    assert not result.allowed
