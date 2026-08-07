from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.tools import (
    ToolInvocation,
    ToolInvocationStatus,
    ToolOutcome,
    ToolOutcomeStatus,
)
from agent_core.tools.executor import ToolRecoveryAction, tool_recovery_action
from tests.contract.support import NOW, RUN_ID, SESSION_ID


def _invocation(
    status: ToolInvocationStatus,
    idempotency: IdempotencyClass = IdempotencyClass.READ_ONLY,
    *,
    effect_sent: bool = False,
    terminal: bool = False,
) -> ToolInvocation:
    return ToolInvocation(
        id=UUID(int=501),
        run_id=RUN_ID,
        session_id=SESSION_ID,
        step_number=1,
        call_id="recovery-call",
        tool_name="demo.recovery",
        tool_version="1.0.0",
        idempotency_class=idempotency,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        status=status,
        raw_arguments="{}",
        idempotency_key="recovery-key",
        effect_sent_at=NOW if effect_sent else None,
        outcome=(
            ToolOutcome(
                status=ToolOutcomeStatus.SUCCEEDED,
                action="demo.recovery",
                reason_code="tool.succeeded",
                message="The tool completed successfully.",
                retryable=False,
                remediation="none",
            )
            if terminal
            else None
        ),
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
    )


SCENARIOS = [
    (_invocation(ToolInvocationStatus.PROPOSED), ToolRecoveryAction.RESUME_AUTHORIZATION),
    (_invocation(ToolInvocationStatus.AUTHORIZED), ToolRecoveryAction.RESUME_AUTHORIZATION),
    (
        _invocation(ToolInvocationStatus.WAITING_FOR_APPROVAL),
        ToolRecoveryAction.RESUME_APPROVAL,
    ),
    (_invocation(ToolInvocationStatus.RUNNING), ToolRecoveryAction.REEXECUTE),
    (
        _invocation(
            ToolInvocationStatus.RUNNING,
            IdempotencyClass.IDEMPOTENT,
            effect_sent=True,
        ),
        ToolRecoveryAction.REEXECUTE,
    ),
    (
        _invocation(
            ToolInvocationStatus.RUNNING,
            IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        ),
        ToolRecoveryAction.REEXECUTE,
    ),
    (
        _invocation(
            ToolInvocationStatus.RUNNING,
            IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
            effect_sent=True,
        ),
        ToolRecoveryAction.REPLAY_IDEMPOTENCY_KEY,
    ),
    (
        _invocation(ToolInvocationStatus.RUNNING, IdempotencyClass.NON_IDEMPOTENT),
        ToolRecoveryAction.REEXECUTE,
    ),
    (
        _invocation(
            ToolInvocationStatus.RUNNING,
            IdempotencyClass.NON_IDEMPOTENT,
            effect_sent=True,
        ),
        ToolRecoveryAction.MARK_UNCERTAIN,
    ),
    (
        _invocation(ToolInvocationStatus.SUCCEEDED, terminal=True),
        ToolRecoveryAction.RETURN_OUTCOME,
    ),
    (
        _invocation(ToolInvocationStatus.FAILED, terminal=True),
        ToolRecoveryAction.RETURN_OUTCOME,
    ),
    (
        _invocation(ToolInvocationStatus.DENIED, terminal=True),
        ToolRecoveryAction.RETURN_OUTCOME,
    ),
    (
        _invocation(ToolInvocationStatus.UNCERTAIN, terminal=True),
        ToolRecoveryAction.RETURN_OUTCOME,
    ),
    (
        _invocation(
            ToolInvocationStatus.RUNNING,
            IdempotencyClass.READ_ONLY,
            effect_sent=True,
        ),
        ToolRecoveryAction.REEXECUTE,
    ),
]


@pytest.mark.parametrize(
    ("invocation", "expected"),
    SCENARIOS,
    ids=[
        f"{invocation.status.value}-{invocation.idempotency_class.value}-"
        f"effect-{invocation.effect_sent_at is not None}"
        for invocation, _expected in SCENARIOS
    ],
)
def test_crash_recovery_action(invocation: ToolInvocation, expected: ToolRecoveryAction) -> None:
    assert tool_recovery_action(invocation) is expected


def test_crash_recovery_table_is_total_across_all_pipeline_boundaries() -> None:
    assert len(SCENARIOS) == 14
