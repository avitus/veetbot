"""Milestone 1 runtime hard gates and acceptance behavior."""

from __future__ import annotations

import ast
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

import agent_core.runtime.executor as executor_module
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.identity import StaticPrincipalResolver
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.config import (
    AuthMode,
    DeploymentMode,
    SandboxMechanism,
    Settings,
    load_settings,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelEvent,
    ModelPermanentError,
    ModelRequest,
    ModelTransientError,
    ModelUsage,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextDeltaEvent,
)
from agent_core.domain.runs import FailureReason, OutcomeKind, RunLimits, RunOutcome, RunStatus
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
    for path in (ROOT / "src" / "agent_core").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and (
                (
                    node.func.attr == "release"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "queue"
                )
                or (
                    node.func.attr == "transition"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr in {"_runs", "runs"}
                )
            )
            for node in ast.walk(tree)
        ):
            transition_modules.add(path.relative_to(ROOT / "src" / "agent_core").as_posix())
    assert transition_modules == {"runtime/executor.py"}


def test_no_ambient_time() -> None:
    assert not [error for error in architecture_errors(ROOT) if "ambient nondeterminism" in error]


def test_no_ambient_id() -> None:
    assert not [
        error for error in architecture_errors(ROOT) if "ambient nondeterminism call uuid." in error
    ]


@pytest.mark.asyncio
async def test_step_identity() -> None:
    for case in load_cases(ROOT / "tests" / "eval_cases"):
        result = await run_case(case, FIXTURE_ROOT)
        step_numbers = {
            event.payload["step_number"]
            for event in result.events
            if event.event_type == "model.request.started"
        }
        assert result.run.step_count == len(step_numbers), case.name


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


@pytest.mark.asyncio
async def test_event_after_terminal_is_a_model_protocol_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_stream = FakeModelProvider.stream

    async def post_terminal_stream(
        provider: FakeModelProvider,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        sequence = 0
        async for event in original_stream(provider, request, resolved, attempt):
            sequence = event.sequence + 1
            yield event
        yield TextDeltaEvent(
            attempt_id=attempt.attempt_id,
            run_id=attempt.run_id,
            step_number=attempt.step_number,
            sequence=sequence,
            item_index=0,
            text="late",
        )

    monkeypatch.setattr(FakeModelProvider, "stream", post_terminal_stream)

    async with build(
        settings=_development_settings(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("reject post-terminal output")
        failed = await composition.runs.wait_terminal(run_id)
    assert failed.status is RunStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.reason is FailureReason.MODEL_PERMANENT_ERROR


@pytest.mark.asyncio
async def test_provider_failure_diagnostics_reach_the_terminal_event_safely() -> None:
    permanent = ModelPermanentError(
        provider="fake",
        model="scripted",
        attempt_id=SequenceIdFactory().new_id(),
        message="the model provider rejected the request",
        provider_code="missing_required_parameter",
        http_status=400,
        provider_parameter="input[12].summary",
    )
    async with build(
        settings=_development_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(fail_with=permanent)]),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("exercise provider diagnostics")
        failed = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
    assert failed.failure is not None
    expected = {
        "provider": "fake",
        "provider_code": "missing_required_parameter",
        "http_status": 400,
        "provider_parameter": "input[12].summary",
    }
    assert failed.failure.details == expected
    response_failure = next(
        event for event in events if event.event_type == "model.response.failed"
    )
    assert {key: response_failure.payload[key] for key in expected} == expected
    terminal = next(event for event in events if event.event_type == "run.failed")
    assert terminal.payload["failure"]["details"] == expected


@pytest.mark.asyncio
async def test_direct_model_error_cannot_persist_an_unsafe_provider_parameter() -> None:
    unsafe_parameter = "input[12].summary\nprovider body"
    permanent = ModelPermanentError(
        provider="fake",
        model="scripted",
        attempt_id=SequenceIdFactory().new_id(),
        message="the model provider rejected the request",
        provider_code="missing_required_parameter",
        http_status=400,
        provider_parameter=unsafe_parameter,
    )
    assert permanent.provider_parameter is None

    async with build(
        settings=_development_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(fail_with=permanent)]),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("reject unsafe provider diagnostics")
        failed = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
    assert failed.failure is not None
    assert "provider_parameter" not in failed.failure.details
    serialized = json.dumps([event.model_dump(mode="json") for event in events])
    assert unsafe_parameter not in serialized


@pytest.mark.asyncio
async def test_post_transition_prologue_failure_reaches_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_resolve(_resolver: StaticPrincipalResolver, _run: object) -> object:
        raise RuntimeError("synthetic principal resolution failure")

    monkeypatch.setattr(StaticPrincipalResolver, "for_run", fail_resolve)
    async with build(
        settings=_development_settings(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("exercise prologue failure")
        failed = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
    assert failed.status is RunStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.reason is FailureReason.INTERNAL_ERROR
    assert "run.failed" in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_final_message_validation_failure_reaches_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def invalid_completion(_context: object) -> RunOutcome:
        return RunOutcome(kind=OutcomeKind.COMPLETED, final_message={"invalid": True})

    monkeypatch.setattr(executor_module, "run_loop", invalid_completion)
    async with build(
        settings=_development_settings(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("exercise finalization failure")
        failed = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
    assert failed.status is RunStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.reason is FailureReason.INTERNAL_ERROR
    assert "run.failed" in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_versioned_attempt_and_identical_call_limits_are_wired(tmp_path: Path) -> None:
    runtime_overlay = tmp_path / "runtime" / "limits.yaml"
    runtime_overlay.parent.mkdir(parents=True)
    runtime_overlay.write_text("model:\n  max_internal_attempts: 1\n", encoding="utf-8")
    tool_overlay = tmp_path / "tools" / "limits.yaml"
    tool_overlay.parent.mkdir(parents=True)
    tool_overlay.write_text("circuit_breaker:\n  identical_call_threshold: 2\n", encoding="utf-8")
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://localhost/runtime",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "dev",
            "SANDBOX_MECHANISM": "fake",
            "AGENT_CONFIG_DIR": str(tmp_path),
            "OPENAI_MODEL": "",
        }
    )

    async with build(
        settings=settings,
        script=FakeModelScript(turns=[ScriptedTurn(), ScriptedTurn(text="too late")]),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("one attempt only")
        attempt_limited = await composition.runs.wait_terminal(run_id)
    assert attempt_limited.status is RunStatus.FAILED
    assert attempt_limited.failure is not None
    assert attempt_limited.failure.reason is FailureReason.EMPTY_MODEL_TURN
    assert attempt_limited.model_call_count == 1

    repeated = ScriptedToolCall(
        name="math.calculate", arguments={"expression": "1 + 1"}, call_id="same-call"
    )
    async with build(
        settings=settings,
        script=FakeModelScript(
            turns=[
                ScriptedTurn(tool_calls=[repeated], stop_reason=StopReason.TOOL_USE),
                ScriptedTurn(tool_calls=[repeated], stop_reason=StopReason.TOOL_USE),
            ]
        ),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
    ) as composition:
        run_id = await composition.runs.submit("repeat the call")
        circuit_broken = await composition.runs.wait_terminal(run_id)
    assert circuit_broken.status is RunStatus.FAILED
    assert circuit_broken.failure is not None
    assert circuit_broken.failure.reason is FailureReason.TOOL_LOOP_DETECTED
    assert circuit_broken.model_call_count == 2


def test_runtime_has_no_provider_adapter_dependency() -> None:
    assert not [
        error
        for error in architecture_errors(ROOT)
        if "provider SDK" in error or "runtime/application reaches" in error
    ]
