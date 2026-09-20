"""Shared behavioral contract for every policy advisor."""

from __future__ import annotations

import inspect
import re

import pytest

from agent_core.adapters.judgment import FakeJudgmentProvider
from agent_core.domain.judgment import JudgmentAnswer, JudgmentRequest, JudgmentResult, NoulAnswer
from agent_core.domain.messages import ModelUsage
from agent_core.domain.policies import AdvisoryVerdict, AdvisoryVerdictType, PolicyDecisionType
from agent_core.policy.judgment_advisor import SHIPPED_POLICY_ADVISORS, JudgmentPolicyAdvisor
from agent_core.ports.policies import PolicyAdvisor
from tests.unit.test_advised_policy_engine import web_action

SIGNAL = re.compile(r"^[a-z][a-z_]{0,63}$")


def _subject(implementation: type, probability: float) -> PolicyAdvisor:
    if implementation is JudgmentPolicyAdvisor:

        def respond(request: JudgmentRequest) -> JudgmentResult:
            answers: dict[str, JudgmentAnswer] = {
                key: NoulAnswer(probability=probability) for key in request.questions
            }
            return JudgmentResult(
                answers=answers, usage=ModelUsage(provider="fake", model="scripted")
            )

        return JudgmentPolicyAdvisor(FakeJudgmentProvider(respond))
    raise AssertionError(f"contract has no subject for {implementation.__name__}")


def test_shipped_advisor_census_is_owned_by_the_production_package() -> None:
    assert SHIPPED_POLICY_ADVISORS
    assert len(set(SHIPPED_POLICY_ADVISORS)) == len(SHIPPED_POLICY_ADVISORS)


def test_an_advisor_cannot_say_allow() -> None:
    assert {verdict.value for verdict in AdvisoryVerdictType} == {
        "abstain",
        "require_approval",
        "deny",
    }
    assert PolicyDecisionType.ALLOW.value not in {verdict.value for verdict in AdvisoryVerdictType}
    assert "modified_arguments" not in AdvisoryVerdict.model_fields


@pytest.mark.parametrize("implementation", SHIPPED_POLICY_ADVISORS)
def test_an_advisor_receives_the_action_and_nothing_else(implementation: type) -> None:
    parameters = list(inspect.signature(implementation.advise).parameters)  # type: ignore[attr-defined]
    assert parameters == ["self", "action"]
    assert list(inspect.signature(PolicyAdvisor.advise).parameters) == ["self", "action"]


@pytest.mark.parametrize("probability", [0.0, 1.0])
@pytest.mark.parametrize("implementation", SHIPPED_POLICY_ADVISORS)
async def test_a_verdict_is_typed_versioned_and_content_free(
    implementation: type, probability: float
) -> None:
    action = web_action(arguments={"query": "distinctive-query-marker"})
    verdict = await _subject(implementation, probability).advise(action)

    assert isinstance(verdict, AdvisoryVerdict)
    assert verdict.verdict in set(AdvisoryVerdictType)
    assert verdict.advisor_version
    assert all(SIGNAL.fullmatch(signal) for signal in verdict.signals)
    assert (verdict.verdict is AdvisoryVerdictType.ABSTAIN) == (not verdict.signals)
    assert "distinctive-query-marker" not in verdict.model_dump_json()


@pytest.mark.parametrize("implementation", SHIPPED_POLICY_ADVISORS)
async def test_no_shipped_advisor_denies(implementation: type) -> None:
    verdict = await _subject(implementation, 1.0).advise(web_action())
    assert verdict.verdict is AdvisoryVerdictType.REQUIRE_APPROVAL
