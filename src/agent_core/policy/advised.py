"""The advisory layer composed behind the policy engine port (ADR-0111).

The deterministic engine stays the authoritative gate. One advisor is
consulted only when that gate returns a plain allow for an action in the
consulted class, and its verdict can only make the decision more restrictive.
An advisor that fails, times out, or is unavailable abstains: the layer is
never load-bearing for safety or for availability.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.policies import (
    AdvisoryVerdictType,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    SideEffectClass,
)
from agent_core.domain.runs import Run
from agent_core.policy.engine import combine_decision_types
from agent_core.ports.determinism import Clock
from agent_core.ports.policies import PolicyAdvisor, PolicyEngine

ADVISORY_TIMEOUT_SECONDS = 0.8
ADVISORY_ESCALATED = "policy.advisory.escalated"
# Where an allowed action still carries model-written text outward: a search
# query, a fetch URL, a navigation URL. Every other class is either already
# escalated by the trust overlay or has no outward side.
_CONSULTED_TARGETS = frozenset({"web_provider", "browser_provider"})
_VERDICT_DECISIONS = {
    AdvisoryVerdictType.REQUIRE_APPROVAL: PolicyDecisionType.REQUIRE_APPROVAL,
    AdvisoryVerdictType.DENY: PolicyDecisionType.DENY,
}


class AdvisoryObserver(Protocol):
    def consulted(
        self, *, tool: str, verdict: str, enforced: bool, signals: tuple[str, ...], seconds: float
    ) -> None: ...

    def abstained(self, *, tool: str, cause: str) -> None: ...


def advisable(action: ProposedAction) -> bool:
    """Whether version one consults the advisor on this action at all."""

    return (
        action.side_effect is SideEffectClass.NETWORK_READ
        and action.target.kind in _CONSULTED_TARGETS
    )


class AdvisedPolicyEngine:
    """The deterministic engine plus one advisor that can only escalate."""

    def __init__(
        self,
        deterministic: PolicyEngine,
        advisor: PolicyAdvisor,
        *,
        enforce: bool,
        clock: Clock,
        observer: AdvisoryObserver | None = None,
        timeout_seconds: float = ADVISORY_TIMEOUT_SECONDS,
    ) -> None:
        self._deterministic = deterministic
        self._advisor = advisor
        self._enforce = enforce
        self._clock = clock
        self._observer = observer
        self._timeout_seconds = timeout_seconds

    async def evaluate(
        self, action: ProposedAction, principal: Principal, run: Run
    ) -> PolicyDecision:
        decision = await self._deterministic.evaluate(action, principal, run)
        # A modified allow passes through untouched: escalating it would ask the
        # owner to approve arguments the deterministic layer had not yet narrowed.
        if decision.decision is not PolicyDecisionType.ALLOW or not advisable(action):
            return decision
        started = self._clock.now()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                verdict = await self._advisor.advise(action)
        except TimeoutError:
            self._abstained(action, "timeout")
            return decision
        except Exception as exc:
            # Failing open is correct here and only here: the advisor can only
            # escalate, so without it the system is exactly as safe as the gate.
            self._abstained(action, type(exc).__name__)
            return decision
        if self._observer is not None:
            self._observer.consulted(
                tool=action.name,
                verdict=verdict.verdict.value,
                enforced=self._enforce,
                signals=verdict.signals,
                seconds=(self._clock.now() - started).total_seconds(),
            )
        advised = _VERDICT_DECISIONS.get(verdict.verdict)
        if advised is None or not self._enforce:
            return decision
        if combine_decision_types(decision.decision, advised) is decision.decision:
            return decision
        return PolicyDecision(
            decision=advised,
            reason_code=ADVISORY_ESCALATED,
            # Signal identifiers and the advisor version only; never content.
            explanation=(
                f"Advisory signals {', '.join(verdict.signals) or 'unspecified'} "
                f"({verdict.advisor_version})."
            ),
            modified_arguments=None,
            policy_version=decision.policy_version,
        )

    def _abstained(self, action: ProposedAction, cause: str) -> None:
        if self._observer is not None:
            self._observer.abstained(tool=action.name, cause=cause)
