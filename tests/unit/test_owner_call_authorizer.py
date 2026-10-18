"""ADR-0213: an owner-requested call needs no confirmation; anything else still asks."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.application.call_authorization import OwnerCallAuthorizer
from agent_core.domain.events import NewEvent
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
    TrustLevel,
)
from agent_core.domain.sessions import Session, SessionStatus
from tests.contract.support import NOW, memory_uow_factory
from tests.unit.test_browser_task_grant_authorizer import RUN, SESSION, WHEN, owner, run

CALL_SCOPES = {"mcp.bland_call.use", "mcp.bland_read.use"}
CALL_ARGUMENTS = {
    "phone_number": "+16505550123",
    "brief": "Remind Riv to throw the ball for Marzipan.",
}


def start_call(**update: Any) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=0xCA),
        tenant_id=owner().tenant_id,
        session_id=SESSION,
        run_id=RUN,
        step_number=1,
        name="mcp.bland_call.start_call",
        summary="Run mcp.bland_call.start_call with validated arguments.",
        side_effect=SideEffectClass.EXTERNAL_MESSAGE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"mcp.bland_call.use"},
        arguments=dict(CALL_ARGUMENTS),
        normalized_arguments_hash="hash",
        origin_trust=TrustLevel.USER,
        target=ExecutionTarget(
            kind="mcp", isolated=False, server_id="bland_call", network_enabled=True
        ),
        evaluated_at=WHEN,
    ).model_copy(update=update)


def schedule_call(**arguments: Any) -> ProposedAction:
    return start_call(
        name="schedule.create",
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        idempotency=IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        required_scopes={"schedule.write"},
        target=ExecutionTarget(kind="builtin", isolated=False, network_enabled=False),
        arguments={
            "title": "Call Riv",
            "instruction": "Call Riv at +16505550123 and remind her to throw the ball.",
            "at": (WHEN + timedelta(hours=1)).isoformat(),
            "place_call": True,
            **arguments,
        },
    )


def approval_required(reason: str = "policy.matrix.external_message") -> PolicyDecision:
    return PolicyDecision(
        decision=PolicyDecisionType.REQUIRE_APPROVAL,
        reason_code=reason,
        explanation="External messages require approval.",
        policy_version="policy-v1",
    )


async def authorize(
    problem: str | None,
    *,
    action: ProposedAction | None = None,
    owner_texts: tuple[str, ...] | None = None,
) -> Any:
    _clock, uow_factory = await memory_uow_factory()
    metadata: dict[str, Any] = {}
    if problem == "scheduler":
        metadata = {"schedule_id": str(UUID(int=9))}
    async with uow_factory() as uow:
        await uow.sessions.create(
            Session(
                id=SESSION,
                tenant_id=owner().tenant_id,
                principal_id=owner().principal_id,
                agent_id=UUID(int=0xA6),
                agent_version="agent-v1",
                status=SessionStatus.ACTIVE,
                metadata=metadata,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        current = run()
        await uow.runs.create(current)
        seed = await uow.events.append(
            NewEvent(
                session_id=SESSION,
                run_id=RUN,
                event_type="user.message.created",
                actor_type={
                    "untrusted_seed": "surface",
                    "scheduler": "scheduler",
                    "unbound_scheduler": "scheduler",
                }.get(problem or "", "principal"),
                actor_id="someone-else" if problem == "different_actor" else owner().principal_id,
                payload={"content": "Call Riv and remind her to throw the ball."},
            )
        )
    current = current.model_copy(update={"seed_event_sequence": seed.sequence})
    if problem == "child":
        current = current.model_copy(update={"parent_run_id": UUID(int=8)})
    principal = owner().model_copy(
        update={
            "scopes": {"schedule.write"}
            if problem == "missing_scope"
            else {
                *CALL_SCOPES,
                "schedule.write",
            }
        }
    )
    proposed = action or start_call()
    if problem in {"external_content", "knowledge_content"}:
        proposed = proposed.model_copy(
            update={
                "origin_trust": TrustLevel.EXTERNAL_UNTRUSTED
                if problem == "external_content"
                else TrustLevel.KNOWLEDGE
            }
        )
    if problem == "memory_lookup":
        proposed = proposed.model_copy(update={"origin_trust": TrustLevel.MEMORY})
    if problem == "other_server":
        proposed = proposed.model_copy(
            update={
                "name": "mcp.gmail_send.send_message",
                "target": ExecutionTarget(
                    kind="mcp", isolated=False, server_id="gmail_send", network_enabled=True
                ),
            }
        )
    return await OwnerCallAuthorizer(uow_factory=uow_factory, now=lambda: WHEN).authorize(
        action=proposed,
        decision=approval_required()
        if problem != "already_allowed"
        else approval_required().model_copy(update={"decision": PolicyDecisionType.ALLOW}),
        principal=principal,
        run=current,
        agent_version="changed" if problem == "agent_changed" else current.agent_version,
        action_deadline=WHEN if problem == "expired" else WHEN + timedelta(seconds=30),
        turn=None
        if problem == "no_turn"
        else AuthorizationTurn(
            newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED
            if problem == "untrusted_turn"
            else TrustLevel.USER,
            owner_texts=owner_texts
            if owner_texts is not None
            else ("Call Riv at +1 (650) 555-0123 and remind her to throw the ball.",)
            if problem != "number_not_from_owner"
            else ("Call Riv and remind her to throw the ball.",),
            conversation_untrusted=problem == "earlier_external_content",
        ),
    )


@pytest.mark.parametrize(
    "problem",
    [
        None,
        "scheduler",
        "memory_lookup",
        "external_content",
        "knowledge_content",
        "untrusted_turn",
        "no_turn",
        "untrusted_seed",
        "unbound_scheduler",
        "different_actor",
        "child",
        "agent_changed",
        "missing_scope",
        "expired",
        "other_server",
        "already_allowed",
        "earlier_external_content",
        "number_not_from_owner",
    ],
)
async def test_owner_call_boundaries(problem: str | None) -> None:
    """The owner's own chat or schedule may call; content-driven or foreign requests ask."""
    result = await authorize(problem)
    assert result.allowed is (problem in {None, "scheduler", "memory_lookup"})
    if result.allowed:
        assert result.reason_code == "call.owner_request.authorized"
        assert result.authorization_kind == "owner_call_request"
        assert result.authorization_ref is not None
        assert result.authorization_view is not None
        assert result.authorization_view["phone_number"] == CALL_ARGUMENTS["phone_number"]


@pytest.mark.parametrize(
    ("arguments", "allowed"),
    [({}, True), ({"place_call": False}, False), ({"use_website": True}, False)],
)
async def test_scheduling_an_owner_call_needs_no_confirmation(
    arguments: dict[str, Any], allowed: bool
) -> None:
    """Asking for a call later is the same request as asking for it now (ADR-0213)."""
    action = schedule_call(**arguments)
    if "place_call" in arguments and not arguments["place_call"]:
        action.arguments.pop("place_call")
    result = await authorize(None, action=action)
    assert result.allowed is allowed


@pytest.mark.parametrize(
    ("instruction", "owner_text", "allowed"),
    [
        # The owner typed the number; the model normalized it.
        ("Call Riv at +16505550123.", "Call Riv at +1 (650) 555-0123 at 6:15.", True),
        ("Call Riv at +16505550123.", "Call Riv at (650) 555-0123 at 6:15.", True),
        ("Call Riv at +16505550123.", "Call Riv at 555-0123 at 6:15.", False),
        ("Call Riv at +16505550123.", "Call Riv at 6:15.", False),
        # Every number the schedule will dial must come from the owner.
        (
            "Call Riv at +16505550123, then +19005550199.",
            "Call Riv at +1 (650) 555-0123 at 6:15.",
            False,
        ),
        ("Call Riv tonight.", "Call Riv at +1 (650) 555-0123 at 6:15.", False),
    ],
)
async def test_a_call_schedule_dials_only_numbers_the_owner_gave(
    instruction: str, owner_text: str, allowed: bool
) -> None:
    """ADR-0213: content cannot pick who a scheduled call dials."""
    result = await authorize(
        None, action=schedule_call(instruction=instruction), owner_texts=(owner_text,)
    )
    assert result.allowed is allowed
