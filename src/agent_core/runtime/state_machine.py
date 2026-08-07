"""The only legal run-state edges."""

from __future__ import annotations

from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import RunStatus

ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.WAITING_FOR_USER,
            RunStatus.QUEUED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_FOR_APPROVAL: frozenset(
        {RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.WAITING_FOR_USER: frozenset(
        {RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def require_transition(current: RunStatus, requested: RunStatus) -> None:
    """Reject any state change not present in the canonical diagram."""

    if requested not in ALLOWED_TRANSITIONS[current]:
        raise ConflictError(f"invalid run transition {current.value}->{requested.value}")
