"""Policy and registration boundaries for fixed web-provider egress."""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import DEFAULT_RULESET
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, run


async def test_operator_selected_web_provider_target_satisfies_network_allowlist() -> None:
    proposed = ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=44),
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name="web.search",
        version="1.0.0",
        summary="Search the public web.",
        side_effect=SideEffectClass.NETWORK_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        arguments={"query": "Ada Lovelace"},
        normalized_arguments_hash="hash",
        origin_trust=TrustLevel.USER,
        target=ExecutionTarget(
            kind="web_provider",
            isolated=False,
            network_enabled=True,
        ),
        evaluated_at=NOW,
    )

    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        proposed,
        principal(),
        run(),
    )

    assert decision.decision is PolicyDecisionType.ALLOW
