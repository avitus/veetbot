"""Milestone 1 runtime hard gates and acceptance behavior."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.messages import (
    FakeModelScript,
    ModelTransientError,
    ModelUsage,
    ScriptedTurn,
)
from agent_core.domain.runs import FailureReason, RunLimits, RunStatus
from agent_core.evals.cases import load_cases
from agent_core.evals.runner import run_case
from scripts.architecture_checks import architecture_errors
from tests.contract.support import NOW

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "models"


def _development_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/runtime",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
    )


def test_one_terminal_writer() -> None:
    transition_modules: set[str] = set()
    for path in (ROOT / "src" / "agent_core" / "runtime").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "transition"
            for node in ast.walk(tree)
        ):
            transition_modules.add(path.name)
    assert transition_modules == {"executor.py"}


def test_no_ambient_time() -> None:
    assert not [error for error in architecture_errors(ROOT) if "ambient nondeterminism" in error]


def test_no_ambient_id() -> None:
    assert not [
        error for error in architecture_errors(ROOT) if "ambient nondeterminism call uuid." in error
    ]


@pytest.mark.asyncio
async def test_step_identity() -> None:
    case = next(
        case
        for case in load_cases(ROOT / "tests" / "eval_cases")
        if case.name == "two_sequential_read_only_tools"
    )
    result = await run_case(case, FIXTURE_ROOT)
    step_numbers = {
        event.payload["step_number"]
        for event in result.events
        if event.event_type == "model.request.started"
    }
    assert result.run.step_count == len(step_numbers) == 3


@pytest.mark.asyncio
async def test_budget_stops() -> None:
    step_case = next(
        case
        for case in load_cases(ROOT / "tests" / "eval_cases")
        if case.name == "step_limit_exceeded"
    )
    step_result = await run_case(step_case, FIXTURE_ROOT)
    assert step_result.run.status is RunStatus.FAILED
    assert step_result.run.failure is not None
    assert step_result.run.failure.reason is FailureReason.MAX_STEPS_EXCEEDED
    assert [event.event_type for event in step_result.events].count("run.queued") == 1

    transient = ModelTransientError(
        provider="fake",
        model="scripted",
        attempt_id=SequenceIdFactory().new_id(),
        message="charged transient failure",
        stream_had_output=False,
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                fail_with=transient,
                usage=ModelUsage(cost=Decimal("1")),
            ),
            ScriptedTurn(text="must not run"),
        ]
    )
    async with build(
        settings=_development_settings(),
        script=script,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        limits=RunLimits(
            max_steps=3,
            max_model_calls=3,
            max_tool_calls=1,
            max_cost=Decimal("1"),
        ),
    ) as composition:
        run_id = await composition.runs.submit("exercise the retry budget")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.reason is FailureReason.BUDGET_EXCEEDED
    assert run.model_call_count == 1
    assert [event.event_type for event in events].count("model.request.started") == 1


@pytest.mark.asyncio
async def test_every_run_transition_has_an_event() -> None:
    for case in load_cases(ROOT / "tests" / "eval_cases"):
        result = await run_case(case, FIXTURE_ROOT)
        event_types = [event.event_type for event in result.events]
        assert "run.started" in event_types
        assert f"run.{result.run.status.value.lower()}" in event_types


@pytest.mark.asyncio
async def test_empty_model_turn_retries_within_one_step() -> None:
    script = FakeModelScript(
        turns=[ScriptedTurn(), ScriptedTurn(), ScriptedTurn(text="Recovered from empty.")]
    )
    async with build(
        settings=_development_settings(),
        script=script,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("recover from an empty model turn")
        run = await composition.runs.wait_terminal(run_id)
    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "Recovered from empty."
    assert run.step_count == 1
    assert run.model_call_count == 3


def test_runtime_has_no_provider_adapter_dependency() -> None:
    assert not [
        error
        for error in architecture_errors(ROOT)
        if "provider SDK" in error or "runtime/application reaches" in error
    ]
