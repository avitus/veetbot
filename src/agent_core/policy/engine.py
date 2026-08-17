"""Pure deterministic policy evaluation and its async port adapter."""

from __future__ import annotations

from agent_core.domain.agents import Principal
from agent_core.domain.policies import (
    ActionKind,
    LoadedRuleset,
    PolicyCondition,
    PolicyDecision,
    PolicyDecisionRank,
    PolicyDecisionType,
    ProposedAction,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run
from agent_core.policy.hardline import hardline_matches

_RANK = {
    PolicyDecisionType.ALLOW: PolicyDecisionRank.ALLOW,
    PolicyDecisionType.ALLOW_WITH_MODIFICATIONS: PolicyDecisionRank.ALLOW_WITH_MODIFICATIONS,
    PolicyDecisionType.REQUIRE_APPROVAL: PolicyDecisionRank.REQUIRE_APPROVAL,
    PolicyDecisionType.DENY: PolicyDecisionRank.DENY,
}


def combine_decision_types(
    deterministic: PolicyDecisionType, advisory: PolicyDecisionType | None
) -> PolicyDecisionType:
    """Combine by maximum restrictiveness; advisory abstention is ``None``."""

    if advisory is None or _RANK[deterministic] >= _RANK[advisory]:
        return deterministic
    return advisory


def _path_inside_workspace(action: ProposedAction) -> bool:
    value = action.arguments.get("path")
    if not isinstance(value, str) or value.startswith("/") or "\\" in value:
        return False
    if value == "":
        return action.target.kind in {"in_process", "sandbox"}
    components = tuple(value.split("/"))
    return (
        action.target.kind in {"in_process", "sandbox"}
        and bool(components)
        and all(component not in {"", ".", ".."} for component in components)
    )


def _condition_holds(condition: PolicyCondition | None, action: ProposedAction) -> bool:
    if condition is None:
        return True
    if condition is PolicyCondition.PATH_INSIDE_WORKSPACE:
        return _path_inside_workspace(action)
    if condition is PolicyCondition.HOST_ON_ALLOWLIST:
        # No authoritative network allowlist is configured in the M4 tier.
        # Model-authored arguments can never grant their own network access.
        return False
    if condition is PolicyCondition.TARGET_ISOLATED:
        return action.target.isolated
    return False


def evaluate_deterministic(
    action: ProposedAction,
    principal: Principal,
    run: Run,
    ruleset: LoadedRuleset,
) -> PolicyDecision:
    """Evaluate without I/O, ambient clocks, mutation, or hidden state."""

    del principal, run
    if action.kind is ActionKind.SKILL_AUTHORING and action.origin_trust is not TrustLevel.USER:
        return PolicyDecision(
            decision=PolicyDecisionType.DENY,
            reason_code="policy.skill.origin_untrusted",
            explanation="Skill authoring requires a user-trusted turn.",
            policy_version=ruleset.policy_version,
        )
    for hardline_rule in ruleset.hardline:
        if hardline_matches(hardline_rule, action):
            return PolicyDecision(
                decision=PolicyDecisionType.DENY,
                reason_code=hardline_rule.message_code,
                explanation=f"Hardline rule {hardline_rule.id} denied the action.",
                policy_version=ruleset.policy_version,
            )
    matching = tuple(rule for rule in ruleset.rules if rule.side_effect is action.side_effect)
    if len(matching) != 1:
        return PolicyDecision(
            decision=ruleset.unknown_tool_decision,
            reason_code="policy.unclassifiable_action",
            explanation="The action could not be classified by the loaded policy profile.",
            policy_version=ruleset.policy_version,
        )
    rule = matching[0]
    allowed = _condition_holds(rule.condition, action)
    decision = rule.decision if allowed else (rule.otherwise or ruleset.default_effect)
    if (
        decision
        in {
            PolicyDecisionType.ALLOW,
            PolicyDecisionType.ALLOW_WITH_MODIFICATIONS,
        }
        and ruleset.external_untrusted_requires_approval
        and (
            action.origin_trust is TrustLevel.EXTERNAL_UNTRUSTED
            or TrustLevel.EXTERNAL_UNTRUSTED in action.argument_trust.values()
        )
        and action.side_effect
        not in {
            SideEffectClass.NONE,
            SideEffectClass.WORKSPACE_READ,
            SideEffectClass.NETWORK_READ,
        }
    ):
        decision = PolicyDecisionType.REQUIRE_APPROVAL
    reason = f"policy.matrix.{action.side_effect.value}"
    return PolicyDecision(
        decision=decision,
        reason_code=reason,
        explanation=f"The {ruleset.profile_name} profile evaluated {action.side_effect.value}.",
        policy_version=ruleset.policy_version,
    )


class DeterministicPolicyEngine:
    def __init__(self, ruleset: LoadedRuleset) -> None:
        self.ruleset = ruleset

    async def evaluate(
        self, action: ProposedAction, principal: Principal, run: Run
    ) -> PolicyDecision:
        return evaluate_deterministic(action, principal, run, self.ruleset)
