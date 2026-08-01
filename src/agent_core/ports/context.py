"""Deterministic context assembly port."""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.messages import ModelRequest
from agent_core.domain.runs import Run, RunCheckpoint


class ContextBuilder(Protocol):
    async def build(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ModelRequest: ...
