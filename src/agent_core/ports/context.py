"""Deterministic context planning, estimation, compaction, and assembly ports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.context import (
    CompactionResult,
    ContextAssembly,
    ContextBudget,
    ContextPlan,
    ContextPressure,
)
from agent_core.domain.messages import ConversationItem, ModelRequest, ResolvedModel
from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.domain.sessions import Session
from agent_core.domain.tools import ToolSpec


class ContextBuilder(Protocol):
    async def build(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ModelRequest: ...


class ContextPlanner(Protocol):
    async def plan(
        self,
        session: Session,
        agent: AgentSpec,
        principal: Principal,
        model: ResolvedModel,
    ) -> ContextPlan: ...

    async def current(self, session_id: UUID) -> ContextPlan | None: ...

    async def rotate(self, session_id: UUID, reason: str) -> ContextPlan: ...


class TokenEstimator(Protocol):
    """A stable estimator whose contiguous-suffix estimates are monotonic."""

    def estimate(self, items: Sequence[ConversationItem], model_id: str) -> int: ...

    def estimate_tools(self, tools: Sequence[ToolSpec], model_id: str) -> int: ...

    def estimate_text(self, text: str, model_id: str) -> int: ...

    def reconcile(self, model_id: str, estimated: int, actual: int) -> None: ...


class Compactor(Protocol):
    async def compact(
        self,
        checkpoint: RunCheckpoint,
        budget: ContextBudget,
        reason: str,
    ) -> tuple[RunCheckpoint, CompactionResult]: ...


class PressureAwareContextBuilder(ContextBuilder, Protocol):
    async def assemble(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ContextAssembly: ...

    async def measure(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ContextPressure: ...
