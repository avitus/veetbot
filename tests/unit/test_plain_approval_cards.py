"""ADR-0260: an approval card says what will happen and why Veetbot is asking.

The headline is plain English, the reason comes from the decision that asked
rather than from the model, and an approval that untrusted content in the turn
caused says so instead of naming a policy code.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.application.public_services import _approval_view
from agent_core.domain.approval_copy import (
    approval_reason,
    generic_action_summary,
    legacy_approval_reason,
)
from agent_core.domain.approvals import ApprovalRequest, ApprovalStatus
from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import ToolSpec
from agent_core.policy.engine import evaluate_deterministic
from agent_core.policy.loader import DEFAULT_RULESET
from agent_core.tools.sandbox_run_command import SandboxRunCommandTool
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, run


def _action(
    side_effect: SideEffectClass,
    *,
    origin_trust: TrustLevel,
    target: ExecutionTarget,
) -> ProposedAction:
    arguments = {"command": ["ls", "-la", "/mnt/data"]}
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID("00000000-0000-0000-0000-0000000025a0"),
        tenant_id=principal().tenant_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=4,
        name="sandbox.run_command",
        version="1.0.0",
        summary="Run a command.",
        side_effect=side_effect,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        arguments=arguments,
        normalized_arguments_hash="hash",
        argument_trust=dict.fromkeys(arguments, TrustLevel.USER),
        origin_trust=origin_trust,
        target=target,
        evaluated_at=NOW,
    )


SANDBOX = ExecutionTarget(kind="sandbox", isolated=True, network_enabled=False)
MAILBOX = ExecutionTarget(kind="in_process", isolated=False, network_enabled=False)


def _evaluate(action: ProposedAction) -> PolicyDecision:
    return evaluate_deterministic(action, principal(), run(), DEFAULT_RULESET)


def _decision(
    reason_code: str = "policy.matrix.external_message", *, untrusted: bool = False
) -> PolicyDecision:
    return PolicyDecision(
        decision=PolicyDecisionType.REQUIRE_APPROVAL,
        reason_code=reason_code,
        explanation="test",
        policy_version="default@test+h00000000",
        untrusted_escalation=untrusted,
    )


# --- the engine says when untrusted content made it ask ---------------------


def test_the_trust_overlay_marks_the_isolated_command_it_escalated() -> None:
    after_web = _evaluate(
        _action(
            SideEffectClass.CODE_EXECUTION,
            origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            target=SANDBOX,
        )
    )
    owner_only = _evaluate(
        _action(SideEffectClass.CODE_EXECUTION, origin_trust=TrustLevel.USER, target=SANDBOX)
    )

    assert after_web.decision is PolicyDecisionType.REQUIRE_APPROVAL
    assert after_web.untrusted_escalation is True
    assert owner_only.decision is PolicyDecisionType.ALLOW
    assert owner_only.untrusted_escalation is False


def test_a_class_that_asks_anyway_is_not_called_an_escalation() -> None:
    decision = _evaluate(
        _action(
            SideEffectClass.EXTERNAL_MESSAGE,
            origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            target=MAILBOX,
        )
    )

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
    assert decision.untrusted_escalation is False


def test_older_stored_decisions_read_as_not_escalated() -> None:
    stored = {
        "decision": "require_approval",
        "reason_code": "policy.matrix.code_execution",
        "explanation": "The default profile evaluated code_execution.",
        "modified_arguments": None,
        "policy_version": "default@old+h00000000",
    }

    assert PolicyDecision.model_validate(stored).untrusted_escalation is False


# --- the reason line ---------------------------------------------------------


def test_an_escalated_command_names_outside_content_as_the_reason() -> None:
    reason = approval_reason(
        _decision("policy.matrix.code_execution", untrusted=True),
        SideEffectClass.CODE_EXECUTION,
    )

    assert reason == (
        "This chat has read web pages or other outside content, "
        "so Veetbot asks before running code."
    )


@pytest.mark.parametrize(
    ("side_effect", "reason"),
    [
        (SideEffectClass.EXTERNAL_MESSAGE, "Veetbot asks before sending a message for you."),
        (SideEffectClass.EXTERNAL_WRITE, "Veetbot asks before changing anything outside Veetbot."),
        (
            SideEffectClass.EXTERNAL_DELETE,
            "Veetbot asks before deleting anything outside Veetbot.",
        ),
        (SideEffectClass.FINANCIAL, "Veetbot asks before spending money."),
        (SideEffectClass.PUBLICATION, "Veetbot asks before publishing anything."),
    ],
)
def test_a_class_that_always_asks_says_what_kind_of_action_it_is(
    side_effect: SideEffectClass, reason: str
) -> None:
    assert approval_reason(_decision(), side_effect) == reason


def test_an_advisory_escalation_says_a_second_check_flagged_it() -> None:
    assert approval_reason(
        _decision("policy.advisory.escalated"), SideEffectClass.NETWORK_READ
    ) == ("A second safety check flagged this request, so Veetbot asks first.")


def test_the_advisory_reason_code_matches_the_advisory_layer() -> None:
    from agent_core.domain import approval_copy
    from agent_core.policy import advised

    assert approval_copy.ADVISORY_ESCALATED == advised.ADVISORY_ESCALATED


def test_a_reason_never_shows_a_policy_code() -> None:
    reason = approval_reason(_decision("policy.tool.demo.thing"), SideEffectClass.NONE)

    assert reason == "Veetbot's safety rules ask for your approval on this action."
    assert "policy." not in reason


def test_older_approvals_translate_their_stored_code() -> None:
    assert (
        legacy_approval_reason("policy.matrix.external_message")
        == "Veetbot asks before sending a message for you."
    )
    assert legacy_approval_reason("policy.matrix.code_execution") == (
        "Veetbot's safety rules ask for your approval on this action."
    )
    assert legacy_approval_reason("policy.advisory.escalated") == (
        "A second safety check flagged this request, so Veetbot asks first."
    )


# --- the headline --------------------------------------------------------------


def _spec(name: str, side_effect: SideEffectClass) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description="test",
        input_schema={"type": "object"},
        output_schema=None,
        side_effect=side_effect,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_seconds=10,
        maximum_output_bytes=1024,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )


@pytest.mark.parametrize(
    ("side_effect", "summary"),
    [
        (SideEffectClass.EXTERNAL_MESSAGE, "Send a message with mcp.chat.post"),
        (SideEffectClass.EXTERNAL_WRITE, "Make a change outside Veetbot with mcp.chat.post"),
        (SideEffectClass.EXTERNAL_DELETE, "Delete something outside Veetbot with mcp.chat.post"),
        (SideEffectClass.FINANCIAL, "Spend money with mcp.chat.post"),
        (SideEffectClass.PUBLICATION, "Publish something with mcp.chat.post"),
        (SideEffectClass.CODE_EXECUTION, "Run code in Veetbot's sandbox with mcp.chat.post"),
        (
            SideEffectClass.WORKSPACE_WRITE,
            "Write a file in this chat's workspace with mcp.chat.post",
        ),
        (SideEffectClass.NETWORK_READ, "Use mcp.chat.post"),
    ],
)
def test_a_tool_without_a_presenter_still_names_the_kind_of_action(
    side_effect: SideEffectClass, summary: str
) -> None:
    assert generic_action_summary(_spec("mcp.chat.post", side_effect)) == summary


async def _sandbox_summary(command: list[str]) -> tuple[str, dict[str, Any]]:
    tool = SandboxRunCommandTool(cast(Any, object()))
    arguments = {"command": command, "timeout_seconds": 10, "working_directory": ""}
    return await tool.approval_view(arguments, tenant_id="tenant")


async def test_a_sandbox_command_card_shows_the_command() -> None:
    summary, arguments = await _sandbox_summary(["ls", "-la", "/mnt/data"])

    assert summary == "Run a command in Veetbot's sandbox: ls -la /mnt/data"
    assert arguments == {
        "command": ["ls", "-la", "/mnt/data"],
        "timeout_seconds": 10,
        "working_directory": "",
    }


async def test_a_shell_script_card_shows_the_script_on_one_line() -> None:
    summary, _ = await _sandbox_summary(
        ["sh", "-lc", "find /workspace/.agent /opt/agent\n  -maxdepth 2 -type f"]
    )

    assert summary == (
        "Run a command in Veetbot's sandbox: find /workspace/.agent /opt/agent -maxdepth 2 -type f"
    )


async def test_a_sandbox_card_never_shows_a_credential() -> None:
    summary, _ = await _sandbox_summary(["curl", "-d", "token=abc123", "x"])

    assert "abc123" not in summary
    assert summary == "Run a command in Veetbot's sandbox: curl -d [REDACTED] x"


async def test_a_long_sandbox_command_is_cut_to_fit_a_glance() -> None:
    summary, _ = await _sandbox_summary(["python3", "-c", "print(1)" * 100])

    assert len(summary) == 160
    assert summary.endswith("…")
    assert summary.startswith("Run a command in Veetbot's sandbox: python3 -c print(1)")


# --- what the API returns -------------------------------------------------------


def _approval(*, policy_reason: str, reason_text: str | None) -> ApprovalRequest:
    return ApprovalRequest(
        id=UUID("00000000-0000-0000-0000-0000000025a1"),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        action_kind=ActionKind.TOOL_CALL,
        action_id=UUID("00000000-0000-0000-0000-0000000025a2"),
        status=ApprovalStatus.PENDING,
        action_summary="Run a command in Veetbot's sandbox: ls -la /mnt/data",
        tool_name="sandbox.run_command",
        arguments={"command": ["ls", "-la", "/mnt/data"]},
        normalized_arguments_hash="hash",
        required_scopes={"sandbox.execute"},
        agent_version="v1",
        risk=RiskLevel.HIGH,
        policy_reason=policy_reason,
        policy_reason_text=reason_text,
        policy_decision=_decision(policy_reason),
        policy_version="default@test+h00000000",
        created_at=NOW,
    )


def test_the_api_returns_the_plain_reason_not_the_policy_code() -> None:
    view = _approval_view(
        _approval(
            policy_reason="policy.matrix.code_execution",
            reason_text="This chat has read web pages or other outside content, "
            "so Veetbot asks before running code.",
        )
    )

    assert view.policy_reason == (
        "This chat has read web pages or other outside content, "
        "so Veetbot asks before running code."
    )


def test_an_older_approval_returns_a_translated_reason() -> None:
    view = _approval_view(_approval(policy_reason="policy.matrix.code_execution", reason_text=None))

    assert view.policy_reason == "Veetbot's safety rules ask for your approval on this action."
