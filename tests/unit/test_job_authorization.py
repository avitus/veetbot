"""Code authoring keeps the trust overlay; widened access needs fresh approval."""

from uuid import UUID

import pytest

from agent_core.application.job_grants import JobGrantAuthorizer
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecisionType,
    SideEffectClass,
    TrustLevel,
)
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.jobs import JobPolicyEngine, job_publication_ruleset
from agent_core.policy.loader import DEFAULT_RULESET
from tests.contract.support import NOW, memory_uow_factory, principal, run
from tests.contract.test_job_store_contract import record
from tests.unit.test_policy_tool_rules import _action


@pytest.mark.parametrize(
    "trust,expected",
    [
        (TrustLevel.USER, PolicyDecisionType.ALLOW),
        (TrustLevel.EXTERNAL_UNTRUSTED, PolicyDecisionType.REQUIRE_APPROVAL),
    ],
)
async def test_job_publication_retains_untrusted_content_approval(
    trust: TrustLevel, expected: PolicyDecisionType
) -> None:
    action = _action(name="job.manage", origin_trust=trust, argument_trust=trust).model_copy(
        update={"side_effect": SideEffectClass.PUBLICATION}
    )
    engine = JobPolicyEngine(
        DeterministicPolicyEngine(DEFAULT_RULESET),
        DeterministicPolicyEngine(job_publication_ruleset(DEFAULT_RULESET)),
    )
    decision = await engine.evaluate(action, principal(), run())
    assert decision.decision is expected
    assert decision.policy_version != DEFAULT_RULESET.policy_version
    unrelated = await engine.evaluate(
        action.model_copy(update={"name": "other.publish"}), principal(), run()
    )
    assert unrelated.policy_version == DEFAULT_RULESET.policy_version
    assert unrelated.decision is PolicyDecisionType.REQUIRE_APPROVAL


async def test_widened_access_and_foreign_grants_do_not_receive_standing_authority() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        await uow.jobs.put(record(principal()), expected_version=None)
    authorizer = JobGrantAuthorizer(factory)
    for extra in (
        {"access": {"inputs": {}, "hosts": {"example.com": None}}},
        {"grant_id": str(UUID(int=999))},
    ):
        action = _action(name="job.run", argument_trust=TrustLevel.USER).model_copy(
            update={
                "arguments": {"slug": "count-items", "revision_id": str(UUID(int=1)), **extra},
                "side_effect": SideEffectClass.EXTERNAL_WRITE,
            }
        )
        decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
            action, principal(), run()
        )
        result = await authorizer.authorize(
            action=action,
            decision=decision,
            principal=principal(),
            run=run(),
            agent_version="1",
            action_deadline=NOW,
            turn=AuthorizationTurn(newest_user_trust=TrustLevel.USER),
        )
        assert not result.allowed


@pytest.mark.parametrize(
    "feeds,expected", [(frozenset(), False), (frozenset({"agent.counts"}), True)]
)
def test_first_feed_grant_needs_approval_but_local_computation_does_not(
    feeds: frozenset[str], expected: bool
) -> None:
    from agent_core.application.job_grants import access_needs_approval
    from agent_core.domain.jobs import JobAccess

    saved = record(principal()).model_copy(update={"grants": {}})
    assert access_needs_approval(JobAccess({}, {}, feeds=feeds), saved) is expected
