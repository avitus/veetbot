"""The tool-name-keyed policy layer: its loader validation and its trust rules.

A tool rule narrows nothing by itself. Suppressing the argument half of the
trust overlay is opt-in per rule through ``human_confirms_arguments``, and the
origin half follows the trust table's "May authorize" column exactly, so a
`MEMORY` or `KNOWLEDGE` origin can no more reach a plain allow than an
`EXTERNAL_UNTRUSTED` one.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import pytest
import yaml

from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    ToolPolicyRule,
    TrustLevel,
)
from agent_core.policy.engine import DeterministicPolicyEngine, evaluate_deterministic
from agent_core.policy.loader import POLICY_DIRECTORY, load_ruleset_documents
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal
from tests.contract.support import run as contract_run

TOOL_NAME = "device.sms.send"


_ABSENT = object()


def _documents() -> tuple[dict[str, Any], dict[str, Any]]:
    profile = yaml.safe_load((POLICY_DIRECTORY / "default.yaml").read_bytes())
    hardline = yaml.safe_load((POLICY_DIRECTORY / "hardline.yaml").read_bytes())
    return copy.deepcopy(profile), copy.deepcopy(hardline)


def _ruleset(tool_rules: Any) -> Any:
    profile, hardline = _documents()
    if tool_rules is _ABSENT:
        profile.pop("tool_rules", None)
    else:
        profile["tool_rules"] = tool_rules
    return load_ruleset_documents(profile, hardline)


def _action(
    *,
    name: str = TOOL_NAME,
    origin_trust: TrustLevel = TrustLevel.USER,
    argument_trust: TrustLevel = TrustLevel.EXTERNAL_UNTRUSTED,
) -> ProposedAction:
    arguments = {"recipient": "+15555550123", "body": "Feeding Marzipan at six."}
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID("00000000-0000-0000-0000-0000000002c0"),
        tenant_id=principal().tenant_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name=name,
        version="1.0.0",
        summary="Compose a text on the paired device.",
        side_effect=SideEffectClass.EXTERNAL_MESSAGE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        arguments=arguments,
        normalized_arguments_hash="hash",
        argument_trust=dict.fromkeys(arguments, argument_trust),
        origin_trust=origin_trust,
        target=ExecutionTarget(kind="device", isolated=False, network_enabled=False),
        evaluated_at=NOW,
    )


# --- the shipped profile ----------------------------------------------------


def test_the_shipped_device_entry_declares_its_human_confirmation() -> None:
    [undeclared] = _ruleset({TOOL_NAME: {"decision": "allow"}}).tool_rules
    assert undeclared.human_confirms_arguments is False

    [shipped] = load_ruleset_documents(*_documents()).tool_rules
    assert shipped.tool_name == TOOL_NAME
    assert shipped.decision is PolicyDecisionType.ALLOW
    assert shipped.human_confirms_arguments is True


def test_an_explicit_empty_otherwise_decision_is_rejected() -> None:
    with pytest.raises(ValueError):
        _ruleset({TOOL_NAME: {"decision": "allow", "otherwise": ""}})


def test_a_non_string_tool_rule_key_is_rejected_as_invalid_policy() -> None:
    with pytest.raises(ValueError):
        _ruleset(
            {
                TOOL_NAME: {"decision": "allow"},
                7: {"decision": "deny"},
            }
        )


# --- (a) the suppression is opt-in per rule ---------------------------------


async def test_an_allow_without_human_confirmation_gets_no_argument_suppression() -> None:
    ruleset = _ruleset({TOOL_NAME: {"decision": "allow"}})

    decision = await DeterministicPolicyEngine(ruleset).evaluate(
        _action(),
        principal(),
        contract_run(),
    )

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


async def test_declared_human_confirmation_suppresses_the_argument_half_only() -> None:
    ruleset = _ruleset({TOOL_NAME: {"decision": "allow", "human_confirms_arguments": True}})

    allowed = await DeterministicPolicyEngine(ruleset).evaluate(
        _action(),
        principal(),
        contract_run(),
    )
    escalated = await DeterministicPolicyEngine(ruleset).evaluate(
        _action(origin_trust=TrustLevel.EXTERNAL_UNTRUSTED),
        principal(),
        contract_run(),
    )

    assert allowed.decision is PolicyDecisionType.ALLOW
    assert escalated.decision is PolicyDecisionType.REQUIRE_APPROVAL


# --- (b) the may-authorize column decides which origins reach a plain allow --


@pytest.mark.parametrize(
    "origin",
    [TrustLevel.PLATFORM, TrustLevel.TRUSTED_CONFIGURATION, TrustLevel.USER],
)
async def test_an_authorizing_origin_reaches_a_plain_allow(origin: TrustLevel) -> None:
    decision = await DeterministicPolicyEngine(
        _ruleset({TOOL_NAME: {"decision": "allow", "human_confirms_arguments": True}})
    ).evaluate(_action(origin_trust=origin), principal(), contract_run())

    assert decision.decision is PolicyDecisionType.ALLOW


@pytest.mark.parametrize(
    "origin",
    [
        TrustLevel.EXTERNAL_UNTRUSTED,
        TrustLevel.MEMORY,
        TrustLevel.KNOWLEDGE,
        TrustLevel.INTERNAL_TOOL,
    ],
)
async def test_a_non_authorizing_origin_escalates_to_approval(origin: TrustLevel) -> None:
    decision = await DeterministicPolicyEngine(
        _ruleset({TOOL_NAME: {"decision": "allow", "human_confirms_arguments": True}})
    ).evaluate(_action(origin_trust=origin), principal(), contract_run())

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


# --- the engine's ambiguity branch ------------------------------------------


def test_two_rules_for_one_tool_name_fail_closed() -> None:
    ruleset = _ruleset({TOOL_NAME: {"decision": "allow", "human_confirms_arguments": True}})
    duplicated = ruleset.model_copy(
        update={
            "tool_rules": (
                *ruleset.tool_rules,
                ToolPolicyRule(
                    tool_name=TOOL_NAME,
                    decision=PolicyDecisionType.ALLOW,
                    human_confirms_arguments=True,
                ),
            )
        },
        deep=True,
    )

    decision = evaluate_deterministic(_action(), principal(), contract_run(), duplicated)

    assert decision.decision is PolicyDecisionType.DENY
    assert decision.reason_code == "policy.unclassifiable_action"


# --- loader validation ------------------------------------------------------


def test_an_absent_section_loads_no_tool_rules() -> None:
    assert _ruleset(_ABSENT).tool_rules == ()


@pytest.mark.parametrize(
    ("tool_rules", "message"),
    [
        (["device.sms.send"], "must be a mapping keyed on tool name"),
        ({"device.*": {"decision": "allow"}}, "is not a valid tool name"),
        ({"Device.Sms.Send": {"decision": "allow"}}, "is not a valid tool name"),
        ({"nodomain": {"decision": "allow"}}, "is not a valid tool name"),
        ({TOOL_NAME: ["allow"]}, "must be a mapping"),
        ({TOOL_NAME: {"decision": "allow", "effect": "allow"}}, "unknown fields"),
        ({TOOL_NAME: {"condition": "target_isolated"}}, "requires a decision"),
        ({TOOL_NAME: {"decision": "maybe"}}, "not a valid PolicyDecisionType"),
        ({TOOL_NAME: {"decision": "allow", "condition": "vibes"}}, "not a valid PolicyCondition"),
        (
            {TOOL_NAME: {"decision": "allow", "human_confirms_arguments": "yes"}},
            "must be boolean",
        ),
    ],
)
def test_malformed_tool_rules_are_refused(tool_rules: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _ruleset(tool_rules)
