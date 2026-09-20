"""Policy decision port."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.policies import (
    AdvisoryVerdict,
    PolicyDecision,
    ProposedAction,
    StandingAuthorization,
)
from agent_core.domain.runs import Run


class PolicyEngine(Protocol):
    async def evaluate(
        self, action: ProposedAction, principal: Principal, run: Run
    ) -> PolicyDecision: ...


class PolicyAdvisor(Protocol):
    """A secondary signal that can only escalate.

    It receives the proposed action and nothing else, so "never sees the rules"
    is a property of this signature: no ruleset, profile, or deterministic
    decision can reach an implementation through it.
    """

    async def advise(self, action: ProposedAction) -> AdvisoryVerdict: ...


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
