"""Policy boundaries for trusted browser-provider reads."""

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
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.registry import validate_registration
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, run


def _browser_action(
    *,
    name: str = "browser.navigate",
    idempotency: IdempotencyClass = IdempotencyClass.READ_ONLY,
) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=56),
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name=name,
        version="1.0.0",
        summary="Navigate an approved browser profile.",
        side_effect=SideEffectClass.NETWORK_READ,
        risk=RiskLevel.LOW,
        idempotency=idempotency,
        arguments={"url": "https://example.org/account"},
        normalized_arguments_hash="hash",
        origin_trust=TrustLevel.USER,
        target=ExecutionTarget(
            kind="browser_provider",
            isolated=True,
            network_enabled=True,
        ),
        evaluated_at=NOW,
    )


async def test_trusted_browser_provider_target_satisfies_network_allowlist() -> None:
    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        _browser_action(),
        principal(),
        run(),
    )

    assert decision.decision is PolicyDecisionType.ALLOW


async def test_browser_act_always_uses_external_write_approval_path() -> None:
    proposed = _browser_action().model_copy(
        update={
            "name": "browser.act",
            "summary": "Click a revision-bound browser element.",
            "side_effect": SideEffectClass.EXTERNAL_WRITE,
            "risk": RiskLevel.HIGH,
            "idempotency": IdempotencyClass.NON_IDEMPOTENT,
            "arguments": {
                "kind": "click",
                "expected_revision": "revision-1",
                "ref": "revision-1:0",
            },
        },
        deep=True,
    )

    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        proposed,
        principal(),
        run(),
    )

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    ("name", "idempotency"),
    [
        ("browser.script", IdempotencyClass.READ_ONLY),
        ("browser.navigate", IdempotencyClass.NON_IDEMPOTENT),
    ],
)
async def test_browser_provider_target_cannot_authorize_arbitrary_egress(
    name: str,
    idempotency: IdempotencyClass,
) -> None:
    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        _browser_action(name=name, idempotency=idempotency),
        principal(),
        run(),
    )

    assert decision.decision is PolicyDecisionType.DENY


def test_browser_act_registration_requires_conservative_write_classification() -> None:
    act = BrowserNavigateTool.spec.model_copy(
        update={
            "name": "browser.act",
            "side_effect": SideEffectClass.EXTERNAL_WRITE,
            "risk": RiskLevel.HIGH,
            "idempotency": IdempotencyClass.NON_IDEMPOTENT,
            "allow_parallel": False,
        },
        deep=True,
    )

    assert validate_registration(act) == act
