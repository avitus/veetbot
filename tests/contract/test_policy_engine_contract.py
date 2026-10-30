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
from agent_core.policy.jobs import JobPolicyEngine, job_publication_ruleset
from agent_core.policy.loader import DEFAULT_RULESET
from agent_core.ports.policies import PolicyEngine
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, run


@pytest.mark.parametrize(
    "engine",
    [
        DeterministicPolicyEngine(DEFAULT_RULESET),
        JobPolicyEngine(
            DeterministicPolicyEngine(DEFAULT_RULESET),
            DeterministicPolicyEngine(job_publication_ruleset(DEFAULT_RULESET)),
        ),
    ],
)
async def test_policy_engine_is_deterministic_for_identical_inputs(engine: PolicyEngine) -> None:
    action = ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=RUN_ID,
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name="demo.external_write",
        version="1.0.0",
        summary="Record a demo write.",
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"demo.write"},
        arguments={"destination": "demo", "content": "hello"},
        normalized_arguments_hash="hash",
        argument_trust={"content": TrustLevel.EXTERNAL_UNTRUSTED},
        origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        target=ExecutionTarget(kind="in_process", isolated=False, network_enabled=False),
        evaluated_at=NOW,
    )
    first = await engine.evaluate(action, principal(), run())
    second = await engine.evaluate(action, principal(), run())
    assert first.model_dump_json() == second.model_dump_json()
    assert first.decision is PolicyDecisionType.REQUIRE_APPROVAL
