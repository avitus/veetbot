"""PostgreSQL proof that delegation works end to end under real durable workers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import pytest

from agent_core.bootstrap import build
from agent_core.config import (
    AuthMode,
    DeploymentMode,
    SandboxMechanism,
    Settings,
)
from agent_core.domain.delegations import DelegationStatus
from agent_core.domain.messages import (
    FakeModelScript,
    ModelUsage,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunKind, RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 24, 17, tzinfo=UTC)


def _delegation_settings() -> Settings:
    base = database_settings()
    return Settings(
        database_url=base.database_url,
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        delegation_enabled=True,
    )


async def test_durable_workers_carry_a_delegation_from_submit_to_completion() -> None:
    """The production path: queue claims, child execution, join, and resume."""

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="delegate.run",
                        arguments={
                            "briefs": [
                                {
                                    "objective": "Compute the delegated sum.",
                                    "success_condition": "A one-line answer.",
                                    "allowed_tools": ["math.calculate"],
                                }
                            ]
                        },
                        call_id="call_delegate_durable",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
                usage=ModelUsage(input_tokens=50, output_tokens=5, cost=Decimal("0.10")),
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "21+21"},
                        call_id="call_child_math",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
                context_contains="<delegation_brief",
                usage=ModelUsage(input_tokens=20, output_tokens=3, cost=Decimal("0.05")),
            ),
            ScriptedTurn(
                text="The delegated sum is 42.",
                stop_reason=StopReason.END_TURN,
                context_contains="42",
                usage=ModelUsage(input_tokens=25, output_tokens=4, cost=Decimal("0.05")),
            ),
            ScriptedTurn(
                text="The child computed 42.",
                stop_reason=StopReason.END_TURN,
                context_contains="The delegated sum is 42.",
                usage=ModelUsage(input_tokens=60, output_tokens=6, cost=Decimal("0.10")),
            ),
        ]
    )
    async with build(
        settings=_delegation_settings(),
        storage="postgres",
        script=script,
        fixed_clock_at=NOW,
    ) as composition:
        run_id = await composition.runs.submit("delegate a sum to a durable child")
        assert (await composition.runs.get(run_id)).status is RunStatus.QUEUED

        interactive = composition.worker_factory("delegation-interactive")
        asynchronous = composition.async_worker_factory("delegation-async")
        assert isinstance(interactive, DurableWorker)
        assert isinstance(asynchronous, DurableWorker)

        assert await interactive.run_once() is True
        parent = await composition.runs.get(run_id)
        assert parent.status is RunStatus.WAITING_FOR_APPROVAL
        assert parent.lease_owner is None
        async with composition.uow_factory() as uow:
            [delegation] = await uow.delegations.get_for_parent_run(run_id)
            assert delegation.status is DelegationStatus.RUNNING
            child_run_id = delegation.children[0].child_run_id
            assert child_run_id is not None
            child = await uow.runs.get(child_run_id, composition.principal)
            assert child.status is RunStatus.QUEUED
            assert child.kind is RunKind.DELEGATED
            assert child.priority == 10

        assert await asynchronous.run_once() is True
        async with composition.uow_factory() as uow:
            child = await uow.runs.get(child_run_id, composition.principal)
            assert child.status is RunStatus.COMPLETED
            assert child.final_message == "The delegated sum is 42."
            child_events = await uow.events.list_after(child.session_id, 0, composition.principal)
            child_event_types = [event.event_type for event in child_events]
            assert "tool.call.started" in child_event_types
            assert "tool.call.completed" in child_event_types
            [delegation] = await uow.delegations.get_for_parent_run(run_id)
            assert delegation.status is DelegationStatus.JOINED
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
            [delegate_invocation] = [
                record for record in invocations if record.tool_name == "delegate.run"
            ]
            assert delegate_invocation.status is ToolInvocationStatus.SUCCEEDED
        parent = await composition.runs.get(run_id)
        assert parent.status is RunStatus.QUEUED

        assert await interactive.run_once() is True
        parent = await composition.runs.get(run_id)
        assert parent.status is RunStatus.COMPLETED
        assert parent.final_message == "The child computed 42."
        async with composition.uow_factory() as uow:
            child = await uow.runs.get(child_run_id, composition.principal)
        assert parent.usage.cost == Decimal("0.20") + child.usage.cost
        assert parent.usage.model_calls == 2 + child.usage.model_calls


class _StagedWorkerCrashError(Exception):
    pass


async def test_a_parent_lost_mid_delegation_is_reclaimed_and_still_joins() -> None:
    """A worker dying between materialization and the park loses no delegation.

    The crash is staged at the parent's finalize: the materializer's commit is
    already durable, the suspension checkpoint is written, and the worker dies
    holding its lease. The reclaim sweep re-queues the parent, the next claim
    replays the pending delegate.run call, the materializer returns the one
    existing delegation, and the run parks, joins, and completes normally.
    """

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="delegate.run",
                        arguments={
                            "briefs": [
                                {
                                    "objective": "Survive a worker crash.",
                                    "success_condition": "A one-line answer.",
                                    "allowed_tools": ["math.calculate"],
                                }
                            ]
                        },
                        call_id="call_delegate_crash",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                text="The crash was survived.",
                stop_reason=StopReason.END_TURN,
                context_contains="<delegation_brief",
            ),
            ScriptedTurn(
                text="The child answered after the crash.",
                stop_reason=StopReason.END_TURN,
                context_contains="The crash was survived.",
            ),
        ]
    )
    async with build(
        settings=_delegation_settings(),
        storage="postgres",
        script=script,
        fixed_clock_at=NOW,
    ) as composition:
        armed = True

        def probe(boundary: str) -> None:
            if armed:
                raise _StagedWorkerCrashError(boundary)

        composition.executor._finalization_write_probe = probe
        run_id = await composition.runs.submit("delegate across a worker crash")
        interactive = composition.worker_factory("crash-interactive")
        asynchronous = composition.async_worker_factory("crash-async")
        assert isinstance(interactive, DurableWorker)
        assert isinstance(asynchronous, DurableWorker)

        with pytest.raises(_StagedWorkerCrashError):
            await interactive.run_once()
        armed = False

        parent = await composition.runs.get(run_id)
        assert parent.status is RunStatus.RUNNING
        assert parent.lease_owner is not None
        async with composition.uow_factory() as uow:
            [delegation] = await uow.delegations.get_for_parent_run(run_id)
            assert delegation.status is DelegationStatus.RUNNING
            child_run_id = delegation.children[0].child_run_id
            assert child_run_id is not None
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
            assert [record.suspended_kind for record in invocations] == ["child_run"]

        clock = composition.clock
        advance = getattr(clock, "advance", None)
        assert callable(advance)
        advance(timedelta(seconds=31))
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            assert await uow.queue.reclaim_expired(100) == 1
        parent = await composition.runs.get(run_id)
        assert parent.status is RunStatus.QUEUED

        # Reclaim schedules the retry with exponential backoff; cross it.
        advance(timedelta(seconds=2))
        assert await interactive.run_once() is True
        parent = await composition.runs.get(run_id)
        assert parent.status is RunStatus.WAITING_FOR_APPROVAL
        assert parent.lease_owner is None
        async with composition.uow_factory() as uow:
            delegations = await uow.delegations.get_for_parent_run(run_id)
            assert len(delegations) == 1
            assert len(delegations[0].children) == 1
            assert delegations[0].children[0].child_run_id == child_run_id

        assert await asynchronous.run_once() is True
        assert (await composition.runs.get(run_id)).status is RunStatus.QUEUED
        assert await interactive.run_once() is True

        parent = await composition.runs.get(run_id)
        assert parent.status is RunStatus.COMPLETED
        assert parent.final_message == "The child answered after the crash."
        assert parent.attempts >= 1
        async with composition.uow_factory() as uow:
            [delegation] = await uow.delegations.get_for_parent_run(run_id)
            assert delegation.status is DelegationStatus.JOINED
            assert delegation.result is not None
            [outcome] = delegation.result.children
            assert outcome.summary == "The crash was survived."


async def test_a_child_past_its_deadline_ends_and_the_delegation_still_joins() -> None:
    """A queued child whose deadline elapses is ended at claim and joins as an error."""

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="delegate.run",
                        arguments={
                            "briefs": [
                                {
                                    "objective": "Finish before a short deadline.",
                                    "success_condition": "A one-line answer.",
                                    "allowed_tools": ["math.calculate"],
                                    "limits": {"wall_seconds": 60},
                                }
                            ]
                        },
                        call_id="call_delegate_deadline",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                text="The child missed its deadline; reporting that.",
                stop_reason=StopReason.END_TURN,
                context_contains="cancelled",
            ),
        ]
    )
    async with build(
        settings=_delegation_settings(),
        storage="postgres",
        script=script,
        fixed_clock_at=NOW,
    ) as composition:
        run_id = await composition.runs.submit("delegate past a short deadline")
        interactive = composition.worker_factory("deadline-interactive")
        asynchronous = composition.async_worker_factory("deadline-async")
        assert isinstance(interactive, DurableWorker)
        assert isinstance(asynchronous, DurableWorker)

        assert await interactive.run_once() is True
        assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        async with composition.uow_factory() as uow:
            [delegation] = await uow.delegations.get_for_parent_run(run_id)
            child_run_id = delegation.children[0].child_run_id
            assert child_run_id is not None
            child = await uow.runs.get(child_run_id, composition.principal)
            assert child.deadline_at is not None
            assert child.deadline_at == delegation.derived_limits[0].deadline_at

        clock = composition.clock
        advance = getattr(clock, "advance", None)
        assert callable(advance)
        advance(child.deadline_at - clock.now() + timedelta(seconds=1))

        assert await asynchronous.run_once() is True
        async with composition.uow_factory() as uow:
            child = await uow.runs.get(child_run_id, composition.principal)
            assert child.status is RunStatus.CANCELLED
            [delegation] = await uow.delegations.get_for_parent_run(run_id)
            assert delegation.status is DelegationStatus.JOINED
            assert delegation.result is not None
            [outcome] = delegation.result.children
            assert outcome.status is RunStatus.CANCELLED
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
            [delegate_invocation] = [
                record for record in invocations if record.tool_name == "delegate.run"
            ]
            assert delegate_invocation.status is ToolInvocationStatus.FAILED
            assert delegate_invocation.outcome is not None
            assert delegate_invocation.outcome.reason_code == "delegation.child_failed"

        assert await interactive.run_once() is True
        parent = await composition.runs.get(run_id)
        assert parent.status is RunStatus.COMPLETED
        assert parent.final_message == "The child missed its deadline; reporting that."
