"""The run state machine admits exactly the documented edges.

Engineering plan 27.2 names the edges out of each state. Resumption is a
re-enqueue rather than a direct return to RUNNING (runtime-loop.md, the resume
ladder), a run may be cancelled or fail while still queued (ADR-0118), and a
running run may be requeued when it yields its lease. Terminal states have no
exits. The table below is written from those documents, not copied from the
implementation, so an edge added or dropped in code fails here.
"""

from __future__ import annotations

import itertools

import pytest

from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import RunStatus
from agent_core.runtime.state_machine import require_transition
from tests.contract.support import RUN_ID, memory_stack, principal, run

Q = RunStatus.QUEUED
R = RunStatus.RUNNING
WA = RunStatus.WAITING_FOR_APPROVAL
WU = RunStatus.WAITING_FOR_USER
C = RunStatus.COMPLETED
F = RunStatus.FAILED
X = RunStatus.CANCELLED

DOCUMENTED_EDGES = {
    (Q, R),
    (Q, F),
    (Q, X),
    (R, WA),
    (R, WU),
    (R, Q),
    (R, C),
    (R, F),
    (R, X),
    (WA, Q),
    (WA, X),
    (WA, F),
    (WU, Q),
    (WU, X),
    (WU, F),
}
ALL_PAIRS = list(itertools.product(RunStatus, repeat=2))


@pytest.mark.parametrize(
    ("current", "requested"),
    [pair for pair in ALL_PAIRS if pair in DOCUMENTED_EDGES],
    ids=lambda status: status.value,
)
def test_documented_edges_are_admitted(current: RunStatus, requested: RunStatus) -> None:
    require_transition(current, requested)


@pytest.mark.parametrize(
    ("current", "requested"),
    [pair for pair in ALL_PAIRS if pair not in DOCUMENTED_EDGES],
    ids=lambda status: status.value,
)
def test_every_other_edge_is_a_conflict(current: RunStatus, requested: RunStatus) -> None:
    with pytest.raises(ConflictError, match=f"{current.value}->{requested.value}"):
        require_transition(current, requested)


@pytest.mark.parametrize("terminal", [C, F, X], ids=lambda status: status.value)
async def test_a_terminal_run_cannot_be_revived_through_its_repository(
    terminal: RunStatus,
) -> None:
    _clock, _sessions, runs, _events = await memory_stack()
    await runs.create(run(status=terminal))

    for requested in RunStatus:
        with pytest.raises(ConflictError):
            await runs.transition(RUN_ID, terminal, requested)

    assert (await runs.get(RUN_ID, principal())).status is terminal


async def test_a_queued_run_cannot_skip_running_to_complete_or_suspend() -> None:
    _clock, _sessions, runs, _events = await memory_stack()
    await runs.create(run())

    for skipped in (C, WA, WU):
        with pytest.raises(ConflictError, match="invalid run transition"):
            await runs.transition(RUN_ID, Q, skipped)

    assert (await runs.get(RUN_ID, principal())).status is Q
