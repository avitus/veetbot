"""Policy decision port."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.policies import PolicyDecision, ProposedAction, StandingAuthorization
from agent_core.domain.runs import Run


class PolicyEngine(Protocol):
    async def evaluate(
        self, action: ProposedAction, principal: Principal, run: Run
    ) -> PolicyDecision: ...


class StandingAuthorizer(Protocol):
    """Optional authority checked only after deterministic approval escalation."""

    async def authorize(
        self,
        *,
        action: ProposedAction,
        decision: PolicyDecision,
        principal: Principal,
        run: Run,
        agent_version: str,
        action_deadline: datetime,
    ) -> StandingAuthorization: ...
