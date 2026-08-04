from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.memory import InMemoryApprovalRepository
from agent_core.domain.approvals import (
    ApprovalRequest,
    ApprovalResolutionState,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.policies import (
    ActionKind,
    PolicyDecision,
    PolicyDecisionType,
    RiskLevel,
)
from tests.contract.support import NOW, PRINCIPAL_ID, RUN_ID, SESSION_ID, TENANT, principal


def request() -> ApprovalRequest:
    return ApprovalRequest(
        id=UUID(int=91),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        action_kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=92),
        tool_invocation_id=UUID(int=92),
        status=ApprovalStatus.PENDING,
        action_summary="Record a demo write.",
        tool_name="demo.external_write",
        arguments={"destination": "demo", "content": "hello"},
        normalized_arguments_hash="hash",
        required_scopes={"demo.write"},
        agent_version="1.0.0",
        risk=RiskLevel.HIGH,
        policy_reason="policy.matrix.external_write",
        policy_decision=PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason_code="policy.matrix.external_write",
            explanation="External writes require approval.",
            policy_version="default@profile+hline",
        ),
        policy_version="default@profile+hline",
        created_at=NOW,
    )


async def test_approval_repository_first_resolution_wins_idempotently() -> None:
    repository = InMemoryApprovalRepository(FixedClock(NOW))
    created = await repository.create(request())
    assert (await repository.get(created.id, principal())).status is ApprovalStatus.PENDING
    first = await repository.resolve(
        created.id, principal(), ApprovalResolutionType.APPROVE_ONCE, None
    )
    same = await repository.resolve(
        created.id, principal(), ApprovalResolutionType.APPROVE_ONCE, None
    )
    different = await repository.resolve(created.id, principal(), ApprovalResolutionType.DENY, None)
    assert first.state is ApprovalResolutionState.APPLIED
    assert same.state is ApprovalResolutionState.ALREADY_RESOLVED_IDENTICALLY
    assert different.state is ApprovalResolutionState.ALREADY_RESOLVED_DIFFERENTLY
    revalidated = await repository.record_revalidation(created.action_id, "default@new+hline")
    assert revalidated.revalidated_policy_version == "default@new+hline"
