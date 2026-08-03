"""Repository and run-scoped identity ports used by Milestone 1."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.messages import ModelUsage
from agent_core.domain.runs import BudgetScope, Run, RunStatus, Step
from agent_core.domain.sessions import Session
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus


class AgentRepository(Protocol):
    async def put(self, agent: AgentSpec) -> None: ...

    async def get_version(self, agent_id: UUID, agent_version: str) -> AgentSpec: ...

    async def latest_version(self, agent_id: UUID) -> AgentSpec: ...


class SessionRepository(Protocol):
    async def create(self, session: Session) -> None: ...

    async def get(self, session_id: UUID, principal: Principal) -> Session: ...


class RunRepository(Protocol):
    async def create(self, run: Run) -> None: ...

    async def get(self, run_id: UUID, principal: Principal) -> Run: ...

    async def transition(
        self,
        run_id: UUID,
        expected_status: RunStatus,
        new_status: RunStatus,
        *,
        failure: object | None = None,
        final_message: str | None = None,
    ) -> Run: ...

    async def update_counters(self, run: Run) -> None: ...


class ToolInvocationRepository(Protocol):
    async def create(self, invocation: ToolInvocation) -> ToolInvocation: ...

    async def find_by_idempotency_key(
        self, run_id: UUID, idempotency_key: str
    ) -> ToolInvocation | None: ...

    async def transition(
        self,
        invocation_id: UUID,
        expected_status: ToolInvocationStatus,
        invocation: ToolInvocation,
    ) -> ToolInvocation: ...

    async def list_for_run(self, run_id: UUID, principal: Principal) -> list[ToolInvocation]: ...


class PrincipalResolver(Protocol):
    async def for_run(self, run: Run) -> Principal: ...


class BudgetLedger(Protocol):
    def check(self, run: Run, scope: BudgetScope) -> None: ...

    async def record_model_usage(self, run: Run, usage: ModelUsage, *, step: Step) -> None: ...

    async def record_tool_usage(self, run: Run, count: int, *, step: Step) -> None: ...

    async def refund_orchestration_turn(self, run: Run, *, step: Step) -> None: ...
