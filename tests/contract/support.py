"""Shared domain factories for port contract suites."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.persistence.memory import (
    InMemoryEventRepository,
    InMemoryRunRepository,
    InMemorySessionRepository,
)
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.policies import ExecutionTarget
from agent_core.domain.runs import Run, RunLimits, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.tools import ToolExecutionContext

NOW = datetime(2026, 7, 25, 13, 3, 11, 482913, tzinfo=UTC)
TENANT = "tenant-a"
PRINCIPAL_ID = "principal-a"
AGENT_ID = UUID("00000000-0000-0000-0000-000000000010")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000020")
RUN_ID = UUID("00000000-0000-0000-0000-000000000030")


def principal() -> Principal:
    return Principal(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        roles={"user"},
        scopes=set(),
    )


def agent(*, max_steps: int = 4) -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="contract agent",
        instructions="Be deterministic.",
        model_policy="fake",
        enabled_tools=["math.calculate", "system.current_time"],
        policy_profile="default",
        limits=RunLimits(max_steps=max_steps, max_model_calls=4, max_tool_calls=4),
    )


def session() -> Session:
    return Session(
        id=SESSION_ID,
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        status=SessionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )


def run(*, status: RunStatus = RunStatus.QUEUED, max_steps: int = 4) -> Run:
    return Run(
        id=RUN_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT,
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        status=status,
        limits=RunLimits(max_steps=max_steps, max_model_calls=4, max_tool_calls=4),
        scheduled_for=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


async def memory_stack() -> tuple[
    FixedClock,
    InMemorySessionRepository,
    InMemoryRunRepository,
    InMemoryEventRepository,
]:
    clock = FixedClock(NOW)
    sessions = InMemorySessionRepository()
    runs = InMemoryRunRepository(sessions, clock)
    events = InMemoryEventRepository(sessions, clock)
    await sessions.create(session())
    return clock, sessions, runs, events


class NeverCancelled:
    reason = None

    def raise_if_cancelled(self) -> None:
        return

    async def wait(self) -> object:
        raise RuntimeError("never-cancelled token cannot finish wait()")


async def no_effect() -> None:
    return


def tool_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        invocation_id=UUID(int=70),
        call_id="call-contract",
        run_id=RUN_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT,
        principal=principal(),
        step_number=1,
        attempt_number=1,
        lease_epoch=0,
        idempotency_key="contract-key",
        deadline_at=NOW,
        timeout_seconds=2,
        maximum_output_bytes=4096,
        target=ExecutionTarget(kind="in_process", isolated=False, network_enabled=False),
        workspace=None,
        artifacts=object(),
        credentials=object(),
        bridge_dispatch=None,
        cancellation=NeverCancelled(),
        mark_effect_sent=no_effect,
    )


def ids() -> SequenceIdFactory:
    return SequenceIdFactory()
