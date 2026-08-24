"""Milestone 13 delegation brief, result, and ledger domain values."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.domain.delegations import (
    ChildOutcome,
    Delegation,
    DelegationBrief,
    DelegationChild,
    DelegationLimits,
    DelegationRequest,
    DelegationReturn,
    DelegationStatus,
)
from agent_core.domain.runs import RunLimits, RunStatus, RunUsage

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
CHILD_RUN_ID = UUID("00000000-0000-0000-0000-000000000141")
CHILD_SESSION_ID = UUID("00000000-0000-0000-0000-000000000142")
ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000143")


def _brief(**updates: object) -> DelegationBrief:
    values: dict[str, object] = {
        "objective": "Collect the three most recent papers on retrieval ranking.",
        "success_condition": "Three citations with one-line relevance notes.",
        "allowed_tools": ["web.search", "web.fetch"],
    }
    values.update(updates)
    return DelegationBrief.model_validate(values)


def test_brief_bounds_objective_success_condition_and_context() -> None:
    brief = _brief(context="Prior notes.", context_refs=[ARTIFACT_ID])

    assert brief.objective.startswith("Collect")
    assert brief.limits is None

    with pytest.raises(ValidationError):
        _brief(objective="")
    with pytest.raises(ValidationError):
        _brief(objective="x" * 4097)
    with pytest.raises(ValidationError):
        _brief(success_condition="")
    with pytest.raises(ValidationError):
        _brief(success_condition="x" * 2049)
    with pytest.raises(ValidationError):
        _brief(context="x" * 16385)
    with pytest.raises(ValidationError):
        _brief(context_refs=[ARTIFACT_ID] * 9)


def test_brief_requires_one_to_sixteen_allowed_tools() -> None:
    with pytest.raises(ValidationError):
        _brief(allowed_tools=[])
    with pytest.raises(ValidationError):
        _brief(allowed_tools=[f"tool.{index}" for index in range(17)])


def test_requested_limits_must_be_positive_when_present() -> None:
    limits = DelegationLimits(max_steps=4, max_cost=Decimal("1.50"))

    assert limits.max_model_calls is None
    assert limits.wall_seconds is None

    for field in ("max_steps", "max_model_calls", "max_tool_calls", "wall_seconds"):
        with pytest.raises(ValidationError):
            DelegationLimits.model_validate({field: 0})
    with pytest.raises(ValidationError):
        DelegationLimits(max_cost=Decimal("0"))


def test_request_orders_briefs_and_defaults_to_the_summary_shape() -> None:
    request = DelegationRequest(briefs=[_brief(), _brief(objective="Second objective.")])

    assert request.return_shape is DelegationReturn.SUMMARY
    assert [brief.objective for brief in request.briefs][1] == "Second objective."

    with pytest.raises(ValidationError):
        DelegationRequest(briefs=[])


def test_child_outcome_requires_a_terminal_status() -> None:
    outcome = ChildOutcome(
        child_run_id=CHILD_RUN_ID,
        child_session_id=CHILD_SESSION_ID,
        status=RunStatus.COMPLETED,
        summary="Found three papers.",
        artifact_refs=[ARTIFACT_ID],
        usage=RunUsage(cost=Decimal("0.25")),
    )

    assert outcome.failure_reason is None

    with pytest.raises(ValidationError):
        ChildOutcome(
            child_run_id=CHILD_RUN_ID,
            child_session_id=CHILD_SESSION_ID,
            status=RunStatus.RUNNING,
        )


def test_ledger_row_carries_children_and_erasure_clears_identifiers() -> None:
    delegation = Delegation(
        id=UUID("00000000-0000-0000-0000-000000000144"),
        tenant_id="tenant-a",
        principal_id="principal-a",
        parent_run_id=UUID("00000000-0000-0000-0000-000000000145"),
        parent_session_id=UUID("00000000-0000-0000-0000-000000000146"),
        invocation_id=UUID("00000000-0000-0000-0000-000000000147"),
        depth=0,
        request=DelegationRequest(briefs=[_brief()]),
        status=DelegationStatus.PENDING,
        children=[
            DelegationChild(
                index=0,
                brief=_brief(),
                derived_limits=RunLimits(deadline_at=NOW),
                granted_scopes=frozenset({"web.read"}),
                child_run_id=CHILD_RUN_ID,
                child_session_id=CHILD_SESSION_ID,
            )
        ],
        created_at=NOW,
    )

    assert delegation.result is None
    assert delegation.joined_at is None
    assert delegation.links_erased_at is None
    assert delegation.children[0].status is None

    erased = delegation.children[0].model_copy(
        update={"child_run_id": None, "child_session_id": None}
    )
    assert erased.child_run_id is None
