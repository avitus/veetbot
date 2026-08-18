"""Policy and registration boundaries for fixed web-provider egress."""

from __future__ import annotations

from uuid import UUID

import pytest

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


def _web_action(
    *,
    name: str = "web.search",
    idempotency: IdempotencyClass = IdempotencyClass.READ_ONLY,
) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=44),
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name=name,
        version="1.0.0",
        summary="Search the public web.",
        side_effect=SideEffectClass.NETWORK_READ,
        risk=RiskLevel.LOW,
        idempotency=idempotency,
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


async def test_operator_selected_web_provider_target_satisfies_network_allowlist() -> None:
    proposed = _web_action()

    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        proposed,
        principal(),
        run(),
    )

    assert decision.decision is PolicyDecisionType.ALLOW


@pytest.mark.parametrize(
    ("name", "idempotency"),
    [
        ("web.crawl", IdempotencyClass.READ_ONLY),
        ("web.search", IdempotencyClass.NON_IDEMPOTENT),
    ],
)
async def test_web_provider_target_cannot_authorize_arbitrary_egress(
    name: str,
    idempotency: IdempotencyClass,
) -> None:
    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        _web_action(name=name, idempotency=idempotency),
        principal(),
        run(),
    )

    assert decision.decision is PolicyDecisionType.DENY
