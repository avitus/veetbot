"""PostgreSQL proof for Milestone 13 delegation materialization."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_core.application.delegations import DelegationMaterializer
from agent_core.bootstrap import Composition, build
from agent_core.domain.delegations import (
    DelegationBrief,
    DelegationCaps,
    DelegationDefaults,
    DelegationRequest,
    DelegationStatus,
)
from agent_core.domain.errors import DelegationValidationError
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind, RunLimits, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus, ToolSpec
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from tests.contract.support import agent
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 24, 16, tzinfo=UTC)
WRITE_BOUNDARIES = (
    "requested_event",
    "agent",
    "session",
    "session_event",
    "run",
    "seed",
    "queued_event",
    "checkpoint",
    "ledger",
    "invocation",
    "materialized_event",
)
PINNED = {
    "math.calculate": ToolSpec(
        name="math.calculate",
        version="1.0.0",
        description="Capability used by the materializer proof.",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema=None,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(),
        timeout_seconds=5,
        maximum_output_bytes=4096,
        allow_parallel=True,
        output_trust=TrustLevel.PLATFORM,
    )
}


class _InjectedCrashError(Exception):
    pass


def _materializer(
    composition: Composition,
    *,
    crash_at: str | None = None,
    caps: DelegationCaps | None = None,
) -> DelegationMaterializer:
    def probe(boundary: str) -> None:
        if boundary == crash_at:
            raise _InjectedCrashError(boundary)

    return DelegationMaterializer(
        uow_factory=composition.uow_factory,
        clock=composition.clock,
        ids=composition.ids,
        seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        defaults=DelegationDefaults(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=8,
            max_cost=Decimal("2"),
            wall_seconds=600,
        ),
        caps=caps
        or DelegationCaps(
            max_children_per_call=3,
            max_live_children_per_parent=8,
            max_depth=1,
            max_live_delegated_runs_per_tenant=16,
            summary_max_bytes=16384,
        ),
        write_probe=probe,
    )


def _request() -> DelegationRequest:
    return DelegationRequest(
        briefs=[
            DelegationBrief(
                objective="Prove this delegation is committed atomically.",
                success_condition="Either everything exists or nothing does.",
                allowed_tools=["math.calculate"],
            )
        ]
    )


def _invocation(invocation_id: UUID, run_id: UUID, session_id: UUID) -> ToolInvocation:
    return ToolInvocation(
        id=invocation_id,
        run_id=run_id,
        session_id=session_id,
        step_number=1,
        call_id=f"delegate-atomic-{invocation_id}",
        tool_name="delegate.run",
        tool_version="1.0.0",
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        status=ToolInvocationStatus.RUNNING,
        raw_arguments="{}",
        idempotency_key=f"delegate-atomic-{invocation_id}",
        created_at=NOW,
        updated_at=NOW,
    )


async def _parent_graph(composition: Composition) -> tuple[Run, ToolInvocation]:
    principal = composition.principal
    session_id, run_id, invocation_id = uuid4(), uuid4(), uuid4()
    deadline = NOW + timedelta(hours=1)
    parent = Run(
        id=run_id,
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_scopes={"workspace.read"},
        agent_id=agent().id,
        agent_version=agent().version,
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
        created_at=NOW,
        updated_at=NOW,
    )
    invocation = _invocation(invocation_id, run_id, session_id)
    async with composition.uow_factory() as uow:
        await uow.agents.put(agent())
        await uow.sessions.create(
            Session(
                id=session_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                agent_id=agent().id,
                agent_version=agent().version,
                status=SessionStatus.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.runs.create(parent)
        await uow.invocations.create(invocation)
    return parent, invocation


async def _session_count(composition: Composition) -> int:
    async with composition.uow_factory() as uow:
        return len(await uow.sessions.list(composition.principal, limit=500))


async def _assert_nothing_survives(
    composition: Composition, parent: Run, invocation_id: UUID, sessions_before: int
) -> None:
    async with composition.uow_factory() as uow:
        assert await uow.delegations.get_by_invocation(invocation_id) is None
        assert await uow.delegations.live_children_for_parent(parent.id) == 0
        invocations = await uow.invocations.list_for_run(parent.id, composition.principal)
        assert [record.suspended_kind for record in invocations] == [None]
    assert await _session_count(composition) == sessions_before


async def test_materialization_rolls_back_at_every_write_boundary_and_retries() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        for boundary in WRITE_BOUNDARIES[:-1]:
            parent, invocation = await _parent_graph(composition)
            sessions_before = await _session_count(composition)

            with pytest.raises(_InjectedCrashError, match=boundary):
                await _materializer(composition, crash_at=boundary).materialize(
                    request=_request(),
                    run=parent,
                    agent=agent(),
                    principal=composition.principal,
                    invocation=invocation,
                    pinned_tools=PINNED,
                )

            await _assert_nothing_survives(composition, parent, invocation.id, sessions_before)

            delegation = await _materializer(composition).materialize(
                request=_request(),
                run=parent,
                agent=agent(),
                principal=composition.principal,
                invocation=invocation,
                pinned_tools=PINNED,
            )
            assert delegation.status is DelegationStatus.RUNNING

            replay = await _materializer(composition).materialize(
                request=_request(),
                run=parent,
                agent=agent(),
                principal=composition.principal,
                invocation=invocation,
                pinned_tools=PINNED,
            )
            assert replay == delegation

            async with composition.uow_factory() as uow:
                [child] = delegation.children
                assert child.child_session_id is not None
                assert child.child_run_id is not None
                child_run = await uow.runs.get(child.child_run_id, composition.principal)
                assert child_run.status is RunStatus.QUEUED
                assert child_run.kind is RunKind.DELEGATED
                assert child_run.parent_run_id == parent.id
                assert child_run.priority == 10
                assert await uow.checkpoints.latest(child.child_run_id) is not None
                events = await uow.events.list_after(
                    child.child_session_id, 0, composition.principal
                )
                assert [event.event_type for event in events] == [
                    "session.created",
                    "user.message.created",
                    "run.queued",
                    "run.checkpointed",
                ]
                invocations = await uow.invocations.list_for_run(parent.id, composition.principal)
                assert [record.suspended_kind for record in invocations] == ["child_run"]


async def test_crash_after_the_final_write_still_commits_nothing() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        parent, invocation = await _parent_graph(composition)
        sessions_before = await _session_count(composition)

        with pytest.raises(_InjectedCrashError, match="materialized_event"):
            await _materializer(composition, crash_at="materialized_event").materialize(
                request=_request(),
                run=parent,
                agent=agent(),
                principal=composition.principal,
                invocation=invocation,
                pinned_tools=PINNED,
            )

        await _assert_nothing_survives(composition, parent, invocation.id, sessions_before)


def _capped_request(objectives: int) -> DelegationRequest:
    return DelegationRequest(
        briefs=[
            DelegationBrief(
                objective=f"Capped objective number {index}.",
                success_condition="A one-line answer.",
                allowed_tools=["math.calculate"],
            )
            for index in range(objectives)
        ]
    )


async def test_fanout_caps_reject_whole_and_tenant_race_serializes() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        tenant = composition.principal.tenant_id
        async with composition.uow_factory() as uow:
            baseline = await uow.delegations.live_children_for_tenant(tenant)
        caps = DelegationCaps(
            max_children_per_call=3,
            max_live_children_per_parent=4,
            max_depth=1,
            max_live_delegated_runs_per_tenant=baseline + 4,
            summary_max_bytes=16384,
        )

        first_parent, first_invocation = await _parent_graph(composition)
        first = await _materializer(composition, caps=caps).materialize(
            request=_capped_request(3),
            run=first_parent,
            agent=agent(),
            principal=composition.principal,
            invocation=first_invocation,
            pinned_tools=PINNED,
        )
        assert len(first.children) == 3

        second_invocation = _invocation(uuid4(), first_parent.id, first_parent.session_id)
        async with composition.uow_factory() as uow:
            await uow.invocations.create(second_invocation)
        with pytest.raises(DelegationValidationError) as parent_capped:
            await _materializer(composition, caps=caps).materialize(
                request=_capped_request(2),
                run=first_parent,
                agent=agent(),
                principal=composition.principal,
                invocation=second_invocation,
                pinned_tools=PINNED,
            )
        assert parent_capped.value.reason == "delegation.fanout_exceeded"
        async with composition.uow_factory() as uow:
            assert await uow.delegations.get_by_invocation(second_invocation.id) is None

        second_parent, second_parent_invocation = await _parent_graph(composition)
        with pytest.raises(DelegationValidationError) as tenant_capped:
            await _materializer(composition, caps=caps).materialize(
                request=_capped_request(2),
                run=second_parent,
                agent=agent(),
                principal=composition.principal,
                invocation=second_parent_invocation,
                pinned_tools=PINNED,
            )
        assert tenant_capped.value.reason == "delegation.tenant_cap"
        async with composition.uow_factory() as uow:
            assert await uow.delegations.get_by_invocation(second_parent_invocation.id) is None

        racer_one, racer_one_invocation = await _parent_graph(composition)
        racer_two, racer_two_invocation = await _parent_graph(composition)

        async def _race(parent: Run, invocation: ToolInvocation) -> object:
            try:
                return await _materializer(composition, caps=caps).materialize(
                    request=_capped_request(1),
                    run=parent,
                    agent=agent(),
                    principal=composition.principal,
                    invocation=invocation,
                    pinned_tools=PINNED,
                )
            except DelegationValidationError as error:
                return error

        outcomes = await asyncio.gather(
            _race(racer_one, racer_one_invocation),
            _race(racer_two, racer_two_invocation),
        )
        errors = [value for value in outcomes if isinstance(value, DelegationValidationError)]
        assert len(errors) == 1
        assert errors[0].reason == "delegation.tenant_cap"
        async with composition.uow_factory() as uow:
            assert await uow.delegations.live_children_for_tenant(tenant) == baseline + 4
