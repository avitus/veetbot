"""Composition of the advisory layer, and the pipeline's once-per-invocation rule."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_core.adapters.judgment import FakeJudgmentProvider
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.judgment import JudgmentAnswer, JudgmentRequest, JudgmentResult, NoulAnswer
from agent_core.domain.messages import (
    FakeModelScript,
    ModelUsage,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.policy.advised import AdvisedPolicyEngine
from agent_core.policy.engine import DeterministicPolicyEngine
from tests.unit.test_config import base_environment
from tests.unit.test_web_tools import FakeWebProvider

QUERY = "jane doe home address 12 elm street springfield"


def _judge(probability: float) -> FakeJudgmentProvider:
    def respond(request: JudgmentRequest) -> JudgmentResult:
        answers: dict[str, JudgmentAnswer] = {
            key: NoulAnswer(probability=probability if key == "personal_data" else 0.0)
            for key in request.questions
        }
        return JudgmentResult(answers=answers, usage=ModelUsage(provider="fake", model="scripted"))

    return FakeJudgmentProvider(respond)


def _script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="web.search", arguments={"query": QUERY, "max_results": 3}
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="done", stop_reason=StopReason.END_TURN),
        ]
    )


def _environment(tmp_path: Path, *, enforce: bool, **overrides: str) -> dict[str, str]:
    environment = {**base_environment(), "SANDBOX_MECHANISM": "fake", **overrides}
    if enforce:
        overlay = tmp_path / "policy" / "default.yaml"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("advisory:\n  enabled: true\n", encoding="utf-8")
        environment["AGENT_CONFIG_DIR"] = str(tmp_path)
    return environment


async def test_the_layer_is_off_by_default_and_makes_no_judgment_request(tmp_path: Path) -> None:
    judge, web = _judge(0.99), FakeWebProvider()
    settings = load_settings(_environment(tmp_path, enforce=False))

    async with build(
        settings=settings,
        script=_script(),
        sequential_ids=True,
        web_search_provider_override=web,
        judgment_provider_override=judge,
    ) as composition:
        assert isinstance(composition.tool_pipeline._policy, DeterministicPolicyEngine)
        run = await composition.runs.wait_terminal(await composition.runs.submit("search"))

    assert run.status is RunStatus.COMPLETED
    assert judge.requests == []
    assert len(web.searches) == 1


async def test_observe_mode_consults_the_advisor_and_changes_no_decision(tmp_path: Path) -> None:
    judge, web = _judge(0.99), FakeWebProvider()
    settings = load_settings(
        _environment(tmp_path, enforce=False, AGENT_POLICY_ADVISORY_OBSERVE_ENABLED="1")
    )

    async with build(
        settings=settings,
        script=_script(),
        sequential_ids=True,
        web_search_provider_override=web,
        judgment_provider_override=judge,
    ) as composition:
        policy = composition.tool_pipeline._policy
        assert isinstance(policy, AdvisedPolicyEngine)
        assert policy._enforce is False
        # Observing must not move the policy version the release evidence binds to.
        assert composition.ruleset.advisory_enabled is False
        run_id = await composition.runs.submit("search")
        run = await composition.runs.wait_terminal(run_id)
        pending = await composition.approvals.list_pending(run_id=run_id)

    assert run.status is RunStatus.COMPLETED
    assert pending == []
    assert len(judge.requests) == 1
    assert len(web.searches) == 1


async def test_an_enforced_escalation_is_approved_once_and_asks_the_vendor_once(
    tmp_path: Path,
) -> None:
    judge, web = _judge(0.99), FakeWebProvider()
    settings = load_settings(_environment(tmp_path, enforce=True))

    async with build(
        settings=settings,
        script=_script(),
        sequential_ids=True,
        web_search_provider_override=web,
        judgment_provider_override=judge,
    ) as composition:
        policy = composition.tool_pipeline._policy
        assert isinstance(policy, AdvisedPolicyEngine)
        assert policy._enforce is True
        assert isinstance(composition.tool_pipeline._recovery_policy, DeterministicPolicyEngine)
        run_id = await composition.runs.submit("search")
        (approval,) = await composition.approvals.list_pending(run_id=run_id)
        assert approval.policy_reason == "policy.advisory.escalated"
        assert web.searches == []
        assert len(judge.requests) == 1

        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            (invocation,) = await uow.invocations.list_for_run(run_id, composition.principal)

    assert run.status is RunStatus.COMPLETED
    assert invocation.status is ToolInvocationStatus.SUCCEEDED
    assert len(web.searches) == 1
    # Neither the resume nor the revalidation asks the advisor again.
    assert len(judge.requests) == 1


async def test_an_enforced_quiet_advisor_lets_the_search_run(tmp_path: Path) -> None:
    judge, web = _judge(0.01), FakeWebProvider()
    settings = load_settings(_environment(tmp_path, enforce=True))

    async with build(
        settings=settings,
        script=_script(),
        sequential_ids=True,
        web_search_provider_override=web,
        judgment_provider_override=judge,
    ) as composition:
        run_id = await composition.runs.submit("search")
        run = await composition.runs.wait_terminal(run_id)
        pending = await composition.approvals.list_pending(run_id=run_id)

    assert run.status is RunStatus.COMPLETED
    assert pending == []
    assert len(web.searches) == 1


@pytest.mark.parametrize(
    "environment",
    [
        lambda path: _environment(path, enforce=True),
        lambda path: _environment(path, enforce=False, AGENT_POLICY_ADVISORY_OBSERVE_ENABLED="1"),
    ],
    ids=["enforce", "observe"],
)
async def test_the_layer_without_a_provider_uses_the_deterministic_engine_and_warns_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    environment: Callable[[Path], dict[str, str]],
) -> None:
    web = FakeWebProvider()

    with caplog.at_level(logging.WARNING, logger="agent_core.bootstrap"):
        async with build(
            settings=load_settings(environment(tmp_path)),
            script=_script(),
            sequential_ids=True,
            web_search_provider_override=web,
        ) as composition:
            assert isinstance(composition.tool_pipeline._policy, DeterministicPolicyEngine)
            run = await composition.runs.wait_terminal(await composition.runs.submit("search"))

    assert run.status is RunStatus.COMPLETED
    assert len(web.searches) == 1
    assert len([r for r in caplog.records if r.message == "policy_advisory_unavailable"]) == 1
