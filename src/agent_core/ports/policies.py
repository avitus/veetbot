"""Policy decision port."""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.policies import PolicyDecision, ProposedAction
from agent_core.domain.runs import Run


class PolicyEngine(Protocol):
    async def evaluate(
        self, action: ProposedAction, principal: Principal, run: Run
    ) -> PolicyDecision: ...
