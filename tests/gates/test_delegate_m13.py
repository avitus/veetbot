"""Milestone 13 delegation gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.application.delegations import DelegationMaterializer
from agent_core.domain.delegations import (
    DelegationBrief,
    DelegationCaps,
    DelegationDefaults,
    DelegationLimits,
    DelegationRequest,
    DelegationStatus,
    derive_child_limits,
)
from agent_core.domain.errors import DelegationValidationError
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind, RunLimits, RunStatus, RunUsage
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus, ToolSpec
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from tests.contract import support
from tests.contract.support import memory_uow_factory, principal

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


INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000160")
CAPS = DelegationCaps(
    max_children_per_call=3,
    max_live_children_per_parent=8,
    max_depth=1,
    max_live_delegated_runs_per_tenant=16,
    summary_max_bytes=16384,
)


def _capability_spec(name: str, scopes: frozenset[str] = frozenset()) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=f"Capability {name} for delegation gates.",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema=None,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(scopes),
        timeout_seconds=5,
        maximum_output_bytes=4096,
        allow_parallel=True,
        output_trust=TrustLevel.PLATFORM,
    )


PINNED = {
    "math.calculate": _capability_spec("math.calculate"),
    "system.current_time": _capability_spec("system.current_time"),
    "workspace.read_file": _capability_spec("workspace.read_file", frozenset({"workspace.read"})),
}


def _request(*briefs: DelegationBrief) -> DelegationRequest:
    return DelegationRequest(briefs=list(briefs))


def _materializer_brief(**updates: object) -> DelegationBrief:
    values: dict[str, object] = {
        "objective": "Summarize the workspace notes on ranking.",
        "success_condition": "A bounded summary naming each note.",
        "allowed_tools": ["math.calculate", "workspace.read_file"],
    }
    values.update(updates)
    return DelegationBrief.model_validate(values)


async def _materializer_stack() -> tuple[
    DelegationMaterializer,
    MemoryUnitOfWorkFactory,
    Run,
    ToolInvocation,
]:
    clock, factory = await memory_uow_factory()
    deadline = support.NOW + timedelta(seconds=3600)
    parent = Run(
        id=support.RUN_ID,
        session_id=support.SESSION_ID,
        tenant_id=support.TENANT,
        principal_scopes={"workspace.read", "session.read"},
        agent_id=support.AGENT_ID,
        agent_version="1.0.0",
        status=RunStatus.RUNNING,
        step_count=1,
        model_call_count=1,
        tool_call_count=1,
        limits=RunLimits(
            max_steps=8,
            max_model_calls=8,
            max_tool_calls=16,
            max_cost=Decimal("10"),
            deadline_at=deadline,
        ),
        deadline_at=deadline,
        created_at=support.NOW,
        updated_at=support.NOW,
    )
    invocation = ToolInvocation(
        id=INVOCATION_ID,
        run_id=parent.id,
        session_id=parent.session_id,
        step_number=1,
        call_id="delegate-gate-call",
        tool_name="delegate.run",
        tool_version="1.0.0",
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        status=ToolInvocationStatus.RUNNING,
        raw_arguments="{}",
        idempotency_key="delegate-gate-key",
        created_at=support.NOW,
        updated_at=support.NOW,
    )
    async with factory() as uow:
        await uow.agents.put(support.agent())
        await uow.runs.create(parent)
        await uow.invocations.create(invocation)
    materializer = DelegationMaterializer(
        uow_factory=factory,
        dispatcher=None,
        clock=clock,
        ids=support.ids(),
        seed_checkpoint=DurableCheckpointSeeder(clock),
        defaults=DelegationDefaults(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=8,
            max_cost=Decimal("2"),
            wall_seconds=600,
        ),
        caps=CAPS,
    )
    return materializer, factory, parent, invocation


async def test_briefs_are_validated_before_anything_exists() -> None:
    """Reject malformed and over-cap requests wholly, behind gate.delegate.brief_schema."""

    with pytest.raises(ValidationError):
        DelegationRequest.model_validate({"briefs": []})
    with pytest.raises(ValidationError):
        _materializer_brief(objective="")
    with pytest.raises(ValidationError):
        _materializer_brief(allowed_tools=[])

    materializer, factory, parent, invocation = await _materializer_stack()
    rejected: list[tuple[Run, DelegationRequest, str]] = [
        (
            parent,
            _request(*(_materializer_brief() for _ in range(4))),
            "delegation.fanout_exceeded",
        ),
        (
            parent,
            _request(_materializer_brief(allowed_tools=["web.search"])),
            "delegation.tools_not_subset",
        ),
        (
            parent,
            _request(_materializer_brief(allowed_tools=["delegate.run"])),
            "delegation.tools_not_subset",
        ),
        (
            parent,
            _request(_materializer_brief(allowed_tools=["skill.manage"])),
            "delegation.tools_not_subset",
        ),
        (
            parent,
            _request(_materializer_brief(context="password: correct-horse-battery")),
            "delegation.brief_invalid",
        ),
        (
            parent,
            _request(
                _materializer_brief(context_refs=[UUID("00000000-0000-0000-0000-000000000161")])
            ),
            "delegation.brief_invalid",
        ),
        (
            parent.model_copy(update={"kind": RunKind.DELEGATED}),
            _request(_materializer_brief()),
            "delegation.depth_exceeded",
        ),
    ]
    for run, request, reason in rejected:
        with pytest.raises(DelegationValidationError) as raised:
            await materializer.materialize(
                request=request,
                run=run,
                agent=support.agent(),
                principal=principal(),
                invocation=invocation,
                pinned_tools=PINNED,
            )
        assert raised.value.reason == reason

    async with factory() as uow:
        assert await uow.delegations.get_by_invocation(INVOCATION_ID) is None
        sessions = await uow.sessions.list(principal(), limit=10)
        assert [session.id for session in sessions] == [support.SESSION_ID]
        invocations = await uow.invocations.list_for_run(parent.id, principal())
        assert [record.suspended_kind for record in invocations] == [None]
        assert await uow.checkpoints.latest(parent.id) is None


async def test_every_child_gets_a_dedicated_session() -> None:
    """Materialize one dedicated session per brief, behind gate.delegate.dedicated_session."""

    materializer, factory, parent, invocation = await _materializer_stack()
    request = _request(
        _materializer_brief(),
        _materializer_brief(objective="Check the arithmetic in the summary."),
    )

    delegation = await materializer.materialize(
        request=request,
        run=parent,
        agent=support.agent(),
        principal=principal(),
        invocation=invocation,
        pinned_tools=PINNED,
    )

    assert delegation.status is DelegationStatus.RUNNING
    assert [child.index for child in delegation.children] == [0, 1]
    child_session_ids = {child.child_session_id for child in delegation.children}
    assert len(child_session_ids) == 2
    assert parent.session_id not in child_session_ids
    async with factory() as uow:
        for child in delegation.children:
            assert child.child_session_id is not None
            assert child.child_run_id is not None
            child_session = await uow.sessions.get(child.child_session_id, principal())
            assert child_session.metadata["run_kind"] == "delegated"
            assert child_session.metadata["parent_run_id"] == str(parent.id)
            assert child_session.metadata["delegation_id"] == str(delegation.id)
            child_run = await uow.runs.get(child.child_run_id, principal())
            assert child_run.kind is RunKind.DELEGATED
            assert child_run.parent_run_id == parent.id
            assert child_run.session_id == child.child_session_id
            assert child_run.status is RunStatus.QUEUED
            assert child_run.priority == 10
            assert child_run.principal_scopes <= parent.principal_scopes
            assert await uow.checkpoints.latest(child.child_run_id) is not None
            events = await uow.events.list_after(child.child_session_id, 0, principal())
            assert [event.event_type for event in events] == [
                "session.created",
                "user.message.created",
                "run.queued",
                "run.checkpointed",
            ]
        active_parent = await uow.runs.active_for_session(parent.session_id, principal())
        assert active_parent is not None and active_parent.id == parent.id
        stored = await uow.delegations.get_by_invocation(INVOCATION_ID)
        assert stored is not None and stored.id == delegation.id
        invocations = await uow.invocations.list_for_run(parent.id, principal())
        assert [record.suspended_kind for record in invocations] == ["child_run"]
        assert [record.suspended_ref for record in invocations] == [str(delegation.id)]

    replay = await materializer.materialize(
        request=request,
        run=parent,
        agent=support.agent(),
        principal=principal(),
        invocation=invocation,
        pinned_tools=PINNED,
    )
    assert replay == delegation
