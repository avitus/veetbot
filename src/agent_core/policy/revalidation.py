"""Pure approval revalidation table."""

from __future__ import annotations

from agent_core.domain.approvals import ApprovalRequest
from agent_core.domain.policies import PolicyDecisionType


def revalidation_denial_reason(
    approval: ApprovalRequest,
    *,
    arguments_hash: str,
    principal_scopes: set[str],
    agent_version: str,
    policy_version: str,
    policy_decision: PolicyDecisionType,
) -> str | None:
    if (
        arguments_hash != approval.normalized_arguments_hash
        or not approval.required_scopes.issubset(principal_scopes)
        or agent_version != approval.agent_version
    ):
        return "policy.revalidation.changed"
    if policy_decision is PolicyDecisionType.DENY:
        return "policy.revalidation.escalated"
    if (
        approval.policy_version != policy_version
        and policy_decision is not PolicyDecisionType.ALLOW
    ):
        return "policy.revalidation.escalated"
    if policy_decision is PolicyDecisionType.ALLOW_WITH_MODIFICATIONS:
        return "policy.revalidation.escalated"
    return None
