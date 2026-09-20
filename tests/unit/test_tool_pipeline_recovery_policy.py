"""A non-deterministic policy is consulted at most once per invocation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.domain.policies import PolicyDecision, PolicyDecisionType
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW, principal, run
from tests.unit.test_advised_policy_engine import web_action

VERSION = "default@000000000000+h00000000"


def _decision(kind: PolicyDecisionType, *, version: str = VERSION) -> PolicyDecision:
    return PolicyDecision(
        decision=kind,
        reason_code=f"policy.test.{kind.value}",
        explanation="scripted",
        policy_version=version,
    )


class _Engine:
    def __init__(self, decision: PolicyDecision) -> None:
        self._decision = decision
        self.calls = 0

    async def evaluate(self, action: object, principal: object, run: object) -> PolicyDecision:
        del action, principal, run
        self.calls += 1
        return self._decision


def _pipeline(
    primary: _Engine, recovery: _Engine | None, *, persisted: PolicyDecision | None, row: bool
) -> ToolPipeline:
    class _Invocations:
        async def find_by_idempotency_key(self, run_id: object, key: str) -> Any:
            del run_id, key
            return SimpleNamespace(policy_decision=persisted) if row else None

    @asynccontextmanager
    async def uow_factory() -> Any:
        yield SimpleNamespace(invocations=_Invocations())

    return ToolPipeline(
        StaticToolRegistry(),
        uow_factory,  # type: ignore[arg-type]
        FixedClock(NOW),
        SequenceIdFactory(),
        policy=primary,
        recovery_policy=recovery,
    )


async def test_a_new_invocation_is_evaluated_by_the_primary_policy() -> None:
    primary = _Engine(_decision(PolicyDecisionType.REQUIRE_APPROVAL))
    recovery = _Engine(_decision(PolicyDecisionType.ALLOW))
    pipeline = _pipeline(primary, recovery, persisted=None, row=False)

    decision = await pipeline._evaluate_once(web_action(), principal(), run(), "key")

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
    assert (primary.calls, recovery.calls) == (1, 0)


async def test_an_existing_invocation_is_never_put_to_the_primary_policy_again() -> None:
    # The advisor would now escalate, but this invocation was already allowed and may be
    # running: a changed verdict must not move it into a forbidden transition.
    primary = _Engine(_decision(PolicyDecisionType.REQUIRE_APPROVAL))
    recovery = _Engine(_decision(PolicyDecisionType.ALLOW))
    pipeline = _pipeline(primary, recovery, persisted=_decision(PolicyDecisionType.ALLOW), row=True)

    decision = await pipeline._evaluate_once(web_action(), principal(), run(), "key")

    assert decision.decision is PolicyDecisionType.ALLOW
    assert (primary.calls, recovery.calls) == (0, 1)


async def test_an_escalation_recorded_before_a_crash_survives_it() -> None:
    primary = _Engine(_decision(PolicyDecisionType.ALLOW))
    recovery = _Engine(_decision(PolicyDecisionType.ALLOW))
    recorded = PolicyDecision(
        decision=PolicyDecisionType.REQUIRE_APPROVAL,
        reason_code="policy.advisory.escalated",
        explanation="Advisory signals personal_data (scripted@1).",
        policy_version=VERSION,
    )
    pipeline = _pipeline(primary, recovery, persisted=recorded, row=True)

    decision = await pipeline._evaluate_once(web_action(), principal(), run(), "key")

    assert decision == recorded
    assert primary.calls == 0


async def test_a_persisted_decision_under_another_policy_version_does_not_bind() -> None:
    primary = _Engine(_decision(PolicyDecisionType.ALLOW))
    recovery = _Engine(_decision(PolicyDecisionType.ALLOW))
    stale = _decision(PolicyDecisionType.REQUIRE_APPROVAL, version="default@111111111111+h11111111")
    pipeline = _pipeline(primary, recovery, persisted=stale, row=True)

    decision = await pipeline._evaluate_once(web_action(), principal(), run(), "key")

    assert decision.decision is PolicyDecisionType.ALLOW


async def test_one_engine_behaves_exactly_as_before_with_no_extra_lookup() -> None:
    primary = _Engine(_decision(PolicyDecisionType.ALLOW))

    class _Exploding:
        def __call__(self) -> Any:
            raise AssertionError("a single-engine pipeline must not look the row up first")

    pipeline = ToolPipeline(
        StaticToolRegistry(),
        _Exploding(),  # type: ignore[arg-type]
        FixedClock(NOW),
        SequenceIdFactory(),
        policy=primary,
    )

    decision = await pipeline._evaluate_once(web_action(), principal(), run(), "key")

    assert decision.decision is PolicyDecisionType.ALLOW
    assert primary.calls == 1
