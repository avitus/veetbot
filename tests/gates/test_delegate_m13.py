"""Milestone 13 delegation gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_core.domain.delegations import (
    DelegationBrief,
    DelegationDefaults,
    DelegationLimits,
    derive_child_limits,
)
from agent_core.domain.errors import DelegationValidationError
from agent_core.domain.runs import Run, RunLimits, RunStatus, RunUsage

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-0000-0000-000000000131")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000132")
AGENT_ID = UUID("00000000-0000-0000-0000-000000000133")

DEFAULTS = DelegationDefaults(
    max_steps=8,
    max_model_calls=8,
    max_tool_calls=16,
    max_cost=Decimal("5"),
    wall_seconds=600,
)


def _parent(
    *,
    max_steps: int,
    step_count: int,
    max_model_calls: int,
    model_call_count: int,
    max_tool_calls: int,
    tool_call_count: int,
    max_cost: Decimal | None,
    cost_used: Decimal,
    deadline_seconds: int | None,
) -> Run:
    deadline = None if deadline_seconds is None else NOW + timedelta(seconds=deadline_seconds)
    limits = RunLimits(
        max_steps=max_steps,
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
        max_cost=max_cost,
        deadline_at=deadline,
    )
    return Run(
        id=RUN_ID,
        session_id=SESSION_ID,
        tenant_id="tenant-a",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        status=RunStatus.RUNNING,
        step_count=step_count,
        model_call_count=model_call_count,
        tool_call_count=tool_call_count,
        limits=limits,
        usage=RunUsage(cost=cost_used),
        deadline_at=deadline,
        created_at=NOW,
        updated_at=NOW,
    )


def _brief(requested: DelegationLimits | None) -> DelegationBrief:
    return DelegationBrief(
        objective="Survey the design corpus for delegation seams.",
        success_condition="A bounded summary naming each seam.",
        allowed_tools=["web.search"],
        limits=requested,
    )


_requested_limits = st.builds(
    DelegationLimits,
    max_steps=st.one_of(st.none(), st.integers(min_value=1, max_value=100)),
    max_model_calls=st.one_of(st.none(), st.integers(min_value=1, max_value=100)),
    max_tool_calls=st.one_of(st.none(), st.integers(min_value=1, max_value=100)),
    max_cost=st.one_of(
        st.none(),
        st.decimals(min_value=Decimal("0.01"), max_value=Decimal("50"), places=2),
    ),
    wall_seconds=st.one_of(st.none(), st.integers(min_value=1, max_value=7200)),
)


@given(
    max_steps=st.integers(min_value=1, max_value=50),
    step_count=st.integers(min_value=0, max_value=60),
    max_model_calls=st.integers(min_value=1, max_value=50),
    model_call_count=st.integers(min_value=0, max_value=60),
    max_tool_calls=st.integers(min_value=1, max_value=50),
    tool_call_count=st.integers(min_value=0, max_value=60),
    max_cost=st.one_of(
        st.none(),
        st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100"), places=2),
    ),
    cost_used=st.decimals(min_value=Decimal("0"), max_value=Decimal("120"), places=2),
    deadline_seconds=st.one_of(st.none(), st.integers(min_value=-600, max_value=7200)),
    requested=st.lists(st.one_of(st.none(), _requested_limits), min_size=1, max_size=3),
)
def _check_generated_child_limits(
    max_steps: int,
    step_count: int,
    max_model_calls: int,
    model_call_count: int,
    max_tool_calls: int,
    tool_call_count: int,
    max_cost: Decimal | None,
    cost_used: Decimal,
    deadline_seconds: int | None,
    requested: list[DelegationLimits | None],
) -> None:
    parent = _parent(
        max_steps=max_steps,
        step_count=step_count,
        max_model_calls=max_model_calls,
        model_call_count=model_call_count,
        max_tool_calls=max_tool_calls,
        tool_call_count=tool_call_count,
        max_cost=max_cost,
        cost_used=cost_used,
        deadline_seconds=deadline_seconds,
    )
    briefs = [_brief(limits) for limits in requested]
    remaining_steps = max_steps - step_count
    remaining_model_calls = max_model_calls - model_call_count
    remaining_tool_calls = max_tool_calls - tool_call_count
    remaining_cost = None if max_cost is None else max_cost - cost_used
    exhausted = (
        remaining_steps <= 0
        or remaining_model_calls <= 0
        or remaining_tool_calls <= 0
        or (remaining_cost is not None and remaining_cost <= 0)
        or (parent.deadline_at is not None and parent.deadline_at <= NOW)
    )

    try:
        derived = derive_child_limits(parent, briefs, DEFAULTS, now=NOW)
    except DelegationValidationError as error:
        assert error.reason == "delegation.budget_insufficient"
        assert exhausted or (remaining_cost is not None and len(briefs) > 1)
        return

    assert not exhausted
    assert len(derived) == len(briefs)
    for child in derived:
        assert 0 < child.max_steps <= remaining_steps
        assert 0 < child.max_model_calls <= remaining_model_calls
        assert 0 < child.max_tool_calls <= remaining_tool_calls
        assert child.max_cost is not None
        assert child.max_cost > 0
        assert child.deadline_at is not None
        assert child.deadline_at > NOW
        if parent.deadline_at is not None:
            assert child.deadline_at <= parent.deadline_at
    if remaining_cost is not None:
        assert sum(child.max_cost for child in derived if child.max_cost) <= remaining_cost


def test_child_limits_are_derived_and_bounded() -> None:
    """Run the generated bounding contract behind gate.delegate.limits_derived."""

    _check_generated_child_limits()

    exhausted_parent = _parent(
        max_steps=4,
        step_count=4,
        max_model_calls=8,
        model_call_count=0,
        max_tool_calls=8,
        tool_call_count=0,
        max_cost=Decimal("2"),
        cost_used=Decimal("0"),
        deadline_seconds=600,
    )
    with pytest.raises(DelegationValidationError) as steps_exhausted:
        derive_child_limits(exhausted_parent, [_brief(None)], DEFAULTS, now=NOW)
    assert steps_exhausted.value.reason == "delegation.budget_insufficient"

    expired_parent = _parent(
        max_steps=4,
        step_count=0,
        max_model_calls=8,
        model_call_count=0,
        max_tool_calls=8,
        tool_call_count=0,
        max_cost=Decimal("2"),
        cost_used=Decimal("0"),
        deadline_seconds=0,
    )
    with pytest.raises(DelegationValidationError) as deadline_exhausted:
        derive_child_limits(expired_parent, [_brief(None)], DEFAULTS, now=NOW)
    assert deadline_exhausted.value.reason == "delegation.budget_insufficient"
