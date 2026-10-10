"""Only a bounded browser scroll gets the owner's unattended gesture permission."""

from pathlib import Path
from typing import Any

import pytest

from agent_core.domain.browser import BrowserAction, BrowserObservation, BrowserProviderError
from agent_core.domain.messages import ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import (
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    ToolPolicyRule,
    TrustLevel,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.policy.engine import evaluate_deterministic
from agent_core.policy.loader import DEFAULT_RULESET
from tests.contract.support import principal, run
from tests.integration.test_browser_task_grant_pipeline import (
    FINAL,
    LessonRuntime,
    navigate,
    pipeline,
)
from tests.unit.test_browser_policy import _browser_action


def scroll_action(**updates: Any) -> ProposedAction:
    return _browser_action().model_copy(
        update={
            "name": "browser.act",
            "side_effect": SideEffectClass.EXTERNAL_WRITE,
            "risk": RiskLevel.HIGH,
            "idempotency": IdempotencyClass.NON_IDEMPOTENT,
            "arguments": {
                "kind": "scroll",
                "expected_revision": "revision-1",
                "ref": "revision-1:region:0",
                "delta_y": 1557,
            },
            **updates,
        },
        deep=True,
    )


def decision(action: ProposedAction) -> PolicyDecisionType:
    return evaluate_deterministic(action, principal(), run(), DEFAULT_RULESET).decision


@pytest.mark.parametrize("delta", [-2000, -1, 1, 1557, 2000])
def test_bounded_scroll_does_not_ask_for_approval(delta: int) -> None:
    action = scroll_action()
    action.arguments["delta_y"] = delta
    assert decision(action) is PolicyDecisionType.ALLOW


def test_scroll_may_use_untrusted_page_references() -> None:
    assert decision(scroll_action(argument_trust={"ref": TrustLevel.EXTERNAL_UNTRUSTED})) is (
        PolicyDecisionType.ALLOW
    )


def test_observing_a_page_does_not_remove_the_owners_scroll_permission() -> None:
    assert (
        decision(
            scroll_action(
                origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
                newest_user_trust=TrustLevel.USER,
                argument_trust={"ref": TrustLevel.EXTERNAL_UNTRUSTED},
            )
        )
        is PolicyDecisionType.ALLOW
    )


def test_an_untrusted_newest_user_message_cannot_borrow_an_older_owner_turn() -> None:
    assert (
        decision(
            scroll_action(
                origin_trust=TrustLevel.USER,
                newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            )
        )
        is PolicyDecisionType.REQUIRE_APPROVAL
    )


@pytest.mark.parametrize(
    "origin", [TrustLevel.EXTERNAL_UNTRUSTED, TrustLevel.MEMORY, TrustLevel.KNOWLEDGE]
)
def test_page_or_memory_cannot_authorize_a_scroll(origin: TrustLevel) -> None:
    assert decision(scroll_action(origin_trust=origin)) is PolicyDecisionType.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "changes",
    [
        {"delta_y": 0},
        {"delta_y": 2001},
        {"delta_y": -2001},
        {"delta_y": "100"},
        {"delta_y": True},
        {"delta_y": 1.5},
        {"kind": "click"},
        {"kind": "type", "value": "publish"},
        {"kind": "press", "key": "Enter"},
        {"kind": "select", "value": "buy"},
        {"kind": "check"},
        {"value": "send"},
        {"key": "Enter"},
        {"script": "submit()"},
        {"expected_revision": ""},
        {"ref": ""},
        {"postcondition": {"script": "submit()"}},
    ],
)
def test_malformed_or_non_scroll_arguments_do_not_get_permission(changes: dict[str, Any]) -> None:
    action = scroll_action()
    action.arguments.update(changes)
    assert decision(action) is not PolicyDecisionType.ALLOW


@pytest.mark.parametrize(
    "updates",
    [
        {"name": "browser.upload"},
        {"target": ExecutionTarget(kind="browser_provider", isolated=False, network_enabled=True)},
        {"target": ExecutionTarget(kind="browser_provider", isolated=True, network_enabled=False)},
        {"target": ExecutionTarget(kind="mcp", isolated=True, network_enabled=True)},
        {"side_effect": SideEffectClass.NETWORK_READ},
        {"idempotency": IdempotencyClass.READ_ONLY},
        {"risk": RiskLevel.LOW},
    ],
)
def test_scroll_permission_requires_the_exact_browser_tool(updates: dict[str, Any]) -> None:
    assert decision(scroll_action(**updates)) is not PolicyDecisionType.ALLOW


def test_scroll_permission_preserves_hardline_and_operator_denies() -> None:
    assert (
        decision(scroll_action(side_effect=SideEffectClass.CREDENTIAL_ACCESS))
        is PolicyDecisionType.DENY
    )

    ruleset = DEFAULT_RULESET.model_copy(
        update={
            "tool_rules": (
                ToolPolicyRule(tool_name="browser.act", decision=PolicyDecisionType.DENY),
            )
        }
    )
    assert (
        evaluate_deterministic(scroll_action(), principal(), run(), ruleset).decision
        is PolicyDecisionType.DENY
    )
    old_profile = DEFAULT_RULESET.model_copy(update={"tool_rules": ()})
    assert (
        evaluate_deterministic(scroll_action(), principal(), run(), old_profile).decision
        is PolicyDecisionType.REQUIRE_APPROVAL
    )


def test_scroll_with_a_valid_observation_postcondition_is_permitted() -> None:
    action = scroll_action()
    action.arguments["postcondition"] = {"role": "button", "name": "Continue", "timeout_ms": 0}
    assert decision(action) is PolicyDecisionType.ALLOW


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("click", {}),
        ("type", {"value": "hello"}),
        ("select", {"value": "buy"}),
        ("check", {}),
        ("press", {"key": "Enter"}),
    ],
)
def test_valid_other_actions_still_ask_after_an_owner_instruction(
    kind: str, extra: dict[str, Any]
) -> None:
    action = scroll_action(newest_user_trust=TrustLevel.USER)
    action.arguments = {
        "kind": kind,
        "expected_revision": "revision-1",
        "ref": "revision-1:0",
        **extra,
    }
    assert decision(action) is PolicyDecisionType.REQUIRE_APPROVAL


@pytest.mark.parametrize("scheduled", [False, True])
async def test_bound_feed_run_scrolls_and_finishes_without_approval(
    tmp_path: Path, scheduled: bool
) -> None:
    scroll = ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name="browser.act",
                arguments={
                    "kind": "scroll",
                    "expected_revision": "lesson-1",
                    "ref": "lesson-1:0",
                    "delta_y": 1557,
                },
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    async with pipeline(tmp_path, [navigate(), scroll, FINAL]) as harness:
        session_id = await harness.scheduled_session() if scheduled else harness.session_id
        run_id = await harness.submit("Summarize the topics in my home feed.", session_id)
        result = await harness.settled(run_id)
        assert result.status is RunStatus.COMPLETED
        assert result.final_message == FINAL.text
        assert await harness.composition.approvals.list_pending(run_id=run_id) == []
        [invocation] = await harness.acts(run_id)
        assert invocation.status is ToolInvocationStatus.SUCCEEDED
        assert invocation.effect_sent_at is not None
        assert len(harness.runtimes[0].actions) == 1
        assert not any(
            event.event_type == "approval.requested" for event in await harness.events(session_id)
        )


@pytest.mark.parametrize("failure", ["stale", "uncertain"])
async def test_scroll_permission_preserves_revision_and_uncertain_dispatch_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original = LessonRuntime.act

    async def uncertain(runtime: LessonRuntime, action: BrowserAction) -> BrowserObservation:
        await original(runtime, action)
        raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)

    if failure == "uncertain":
        monkeypatch.setattr(LessonRuntime, "act", uncertain)
    scroll = ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name="browser.act",
                arguments={
                    "kind": "scroll",
                    "expected_revision": "lesson-0" if failure == "stale" else "lesson-1",
                    "ref": "lesson-1:0",
                    "delta_y": 1557,
                },
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    async with pipeline(tmp_path, [navigate(), scroll, FINAL]) as harness:
        run_id = await harness.submit("Read more of my feed.")
        result = await harness.settled(run_id)
        assert result.status is RunStatus.COMPLETED
        [invocation] = await harness.acts(run_id)
        assert invocation.status is (
            ToolInvocationStatus.FAILED if failure == "stale" else ToolInvocationStatus.UNCERTAIN
        )
        assert len(harness.runtimes[0].actions) == (0 if failure == "stale" else 1)
        assert await harness.composition.approvals.list_pending(run_id=run_id) == []
