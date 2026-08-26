import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

import agent_core.bootstrap as bootstrap_module
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.domain.evaluations import EvalScenarioRun
from agent_core.domain.runs import FailureReason, RunStatus
from agent_core.evals.capability import (
    CapabilityBudget,
    CapabilityExecution,
    _live_execution,
    load_scenarios,
    resolve_build_ref,
    run_suite,
)
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.unit.test_capability_eval import NOW, EvalUnitOfWorkFactory, _fixture


def _ids(start: int = 3000) -> SequenceIdFactory:
    return SequenceIdFactory(UUID(int=value) for value in range(start, start + 200))


def _execution(
    *,
    provider: str,
    model: str,
    output: str,
    run_id: int = 500,
    cost: str = "0.05",
) -> CapabilityExecution:
    return CapabilityExecution(
        run_id=UUID(int=run_id),
        status=RunStatus.COMPLETED,
        output=output,
        provider=provider,
        model=model,
        model_calls=1,
        tool_calls=0,
        cost_usd=Decimal(cost),
        policy_failures=0,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
    )


def _judge_output() -> str:
    return json.dumps(
        {
            "criteria": [
                {"criterion": "correctness", "observation": "Supported.", "value": 4},
                {"criterion": "clarity", "observation": "Clear.", "value": 3},
            ]
        }
    )


def test_build_ref_resolution_bounds_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIRCLE_SHA1", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    git_executable = "/test/bin/git"
    monkeypatch.setattr(shutil, "which", lambda _name: git_executable)

    def time_out(command: list[str], *_args: object, **_kwargs: object) -> None:
        assert command == [git_executable, "rev-parse", "HEAD"]
        raise subprocess.TimeoutExpired(("git",), 1)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(ValueError, match="could not resolve a build ref"):
        resolve_build_ref(tmp_path, None)


def test_build_ref_resolution_normalizes_git_execution_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CIRCLE_SHA1", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    git_executable = "/test/bin/git"
    monkeypatch.setattr(shutil, "which", lambda _name: git_executable)

    def fail_execution(command: list[str], *_args: object, **_kwargs: object) -> None:
        assert command == [git_executable, "rev-parse", "HEAD"]
        raise OSError("git is unavailable")

    monkeypatch.setattr(subprocess, "run", fail_execution)

    with pytest.raises(ValueError, match="could not resolve a build ref"):
        resolve_build_ref(tmp_path, None)


async def test_daily_cost_stop_uses_persisted_evaluation_aggregate(tmp_path: Path) -> None:
    _fixture(tmp_path)
    factory = EvalUnitOfWorkFactory()
    historical = EvalScenarioRun(
        id=UUID(int=900),
        scenario_id="cap-history-0001",
        suite="research",
        repeat_index=0,
        run_id=UUID(int=901),
        judge_version="judge.v1",
        build_ref="previous",
        score=Decimal("1"),
        policy_failures=0,
        cost_usd=Decimal("2"),
        started_at=NOW,
        finished_at=NOW,
    )
    await factory.repository.replace(historical, [])

    async def execute(*_args: object, **_kwargs: object) -> CapabilityExecution:
        raise AssertionError("daily ceiling should stop before execution")

    result = await run_suite(
        tmp_path,
        suite="research",
        build_ref="abc123",
        uow_factory=cast(UnitOfWorkFactory, factory),
        execute=execute,
        clock=FixedClock(NOW),
        ids=_ids(),
    )

    assert result.release_blocked
    assert result.stopped_by == "daily_cost_usd"
    assert result.runs == ()


async def test_suite_cost_stop_is_release_blocking(tmp_path: Path) -> None:
    _fixture(tmp_path)
    factory = EvalUnitOfWorkFactory()

    async def execute(*_args: object, **_kwargs: object) -> CapabilityExecution:
        return _execution(
            provider="openai",
            model="gpt-5.6-sol",
            output="Subject output.",
            cost="0.50",
        )

    result = await run_suite(
        tmp_path,
        suite="research",
        build_ref="abc123",
        uow_factory=cast(UnitOfWorkFactory, factory),
        execute=execute,
        clock=FixedClock(NOW),
        ids=_ids(3200),
    )

    assert result.release_blocked
    assert result.stopped_by == "suite_cost_usd"


@pytest.mark.parametrize(
    ("subject_provider", "judge_provider", "judge_model", "message"),
    [
        (
            "anthropic",
            "anthropic",
            "claude-opus-5",
            "uses the subject provider as judge",
        ),
        ("openai", "anthropic", "wrong-model", "judge pin mismatch"),
        ("openai", "wrong-provider", "claude-opus-5", "judge pin mismatch"),
    ],
)
async def test_invalid_judge_boundaries_emit_blocking_terminal_event(
    tmp_path: Path,
    subject_provider: str,
    judge_provider: str,
    judge_model: str,
    message: str,
) -> None:
    _fixture(tmp_path)
    factory = EvalUnitOfWorkFactory()

    async def execute(
        model_policy: str, _tools: object, _budget: object, _prompt: str
    ) -> CapabilityExecution:
        if model_policy == "balanced":
            return _execution(
                provider=subject_provider,
                model="subject-model",
                output="Subject output.",
            )
        return _execution(
            provider=judge_provider,
            model=judge_model,
            output=_judge_output(),
            run_id=501,
        )

    with pytest.raises(ValueError, match=message):
        await run_suite(
            tmp_path,
            suite="research",
            build_ref="abc123",
            uow_factory=cast(UnitOfWorkFactory, factory),
            execute=execute,
            clock=FixedClock(NOW),
            ids=_ids(3400),
        )

    events = await factory.process_events.list("eval.suite.completed")
    assert len(events) == 1
    assert events[0].payload["release_blocked"] is True
    assert events[0].payload["stopped_by"] == "evaluation_error"


async def test_non_ceiling_subject_failure_emits_blocking_terminal_event(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    factory = EvalUnitOfWorkFactory()

    async def execute(*_args: object, **_kwargs: object) -> CapabilityExecution:
        return CapabilityExecution(
            run_id=UUID(int=510),
            status=RunStatus.FAILED,
            output=None,
            provider="openai",
            model="gpt-5.6-sol",
            model_calls=1,
            tool_calls=0,
            cost_usd=Decimal("0.05"),
            policy_failures=0,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
        )

    with pytest.raises(ValueError, match="capability subject did not complete"):
        await run_suite(
            tmp_path,
            suite="research",
            build_ref="abc123",
            uow_factory=cast(UnitOfWorkFactory, factory),
            execute=execute,
            clock=FixedClock(NOW),
            ids=_ids(3500),
        )

    events = await factory.process_events.list("eval.suite.completed")
    assert len(events) == 1
    assert events[0].payload["release_blocked"] is True
    assert events[0].payload["stopped_by"] == "evaluation_error"


def test_trajectory_provenance_mismatch_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path)
    path = tmp_path / "evals" / "capability" / "fixtures" / "trajectories" / "failed.json"
    trajectory = json.loads(path.read_text(encoding="utf-8"))
    trajectory["run_id"] = str(UUID(int=999))
    path.write_text(json.dumps(trajectory), encoding="utf-8")

    with pytest.raises(ValueError, match=r"trajectory source disagrees.*run_id"):
        load_scenarios(tmp_path, "research")


def test_milestone_thirteen_capability_scenario_is_accepted(tmp_path: Path) -> None:
    _fixture(tmp_path)
    path = tmp_path / "evals" / "capability" / "scenarios" / "research.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("milestone: 3", "milestone: 13"),
        encoding="utf-8",
    )

    _settings, scenarios = load_scenarios(tmp_path, "research")

    assert scenarios[0].scenario.milestone == 13


def test_milestone_fourteen_capability_scenario_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path)
    path = tmp_path / "evals" / "capability" / "scenarios" / "research.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("milestone: 3", "milestone: 14"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="less than or equal to 13"):
        load_scenarios(tmp_path, "research")


def test_repository_research_scenario_is_admitted_from_failed_trajectory() -> None:
    repository_root = Path(__file__).resolve().parents[2]

    settings, scenarios = load_scenarios(repository_root, "research")

    assert len(scenarios) == 1
    assert settings.daily_cost_usd == Decimal("50.00")
    assert settings.suites["research"].cost_usd == Decimal("25.00")
    scenario = scenarios[0].scenario
    assert scenario.milestone == 13
    assert scenario.ceiling.cost_usd == Decimal("5.00")
    assert scenario.source.outcome == "FAILED"
    assert "independent parallel work" in scenario.source.diagnosis


async def test_tied_cost_ceiling_uses_scenario_scope(tmp_path: Path) -> None:
    _fixture(tmp_path)
    config = tmp_path / "evals" / "capability" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace('daily_cost_usd: "2.00"', 'daily_cost_usd: "0.50"')
        .replace('cost_usd: "1.00"', 'cost_usd: "0.50"'),
        encoding="utf-8",
    )
    factory = EvalUnitOfWorkFactory()

    async def execute(
        _model_policy: str, _tools: object, budget: CapabilityBudget, _prompt: str
    ) -> CapabilityExecution:
        return CapabilityExecution(
            run_id=UUID(int=610),
            status=RunStatus.FAILED,
            output=None,
            provider="openai",
            model="gpt-5.6-sol",
            model_calls=1,
            tool_calls=0,
            cost_usd=budget.cost_usd,
            policy_failures=0,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            failure_reason=FailureReason.BUDGET_EXCEEDED,
        )

    result = await run_suite(
        tmp_path,
        suite="research",
        build_ref="abc123",
        uow_factory=cast(UnitOfWorkFactory, factory),
        execute=execute,
        clock=FixedClock(NOW),
        ids=_ids(3950),
    )

    assert result.runs[0].run.ceiling_hit == "cost_usd"


async def test_replaying_build_uses_canonical_process_event_keys(tmp_path: Path) -> None:
    _fixture(tmp_path)
    factory = EvalUnitOfWorkFactory()
    subject_run_id = 500

    async def execute(
        model_policy: str, _tools: object, _budget: object, _prompt: str
    ) -> CapabilityExecution:
        if model_policy == "balanced":
            return _execution(
                provider="openai",
                model="gpt-5.6-sol",
                output="Subject output.",
                run_id=subject_run_id,
            )
        return _execution(
            provider="anthropic",
            model="claude-opus-5",
            output=_judge_output(),
            run_id=501,
        )

    for start in (3600, 3800):
        await run_suite(
            tmp_path,
            suite="research",
            build_ref="abc123",
            uow_factory=cast(UnitOfWorkFactory, factory),
            execute=execute,
            clock=FixedClock(NOW),
            ids=_ids(start),
        )

    events = await factory.process_events.list()
    assert len(events) == 3
    assert len({event.derivation_key for event in events}) == 3

    subject_run_id = 600
    await run_suite(
        tmp_path,
        suite="research",
        build_ref="abc123",
        uow_factory=cast(UnitOfWorkFactory, factory),
        execute=execute,
        clock=FixedClock(NOW),
        ids=_ids(3900),
    )

    replacement_events = await factory.process_events.list()
    assert len(replacement_events) == 6


async def test_live_execution_bounds_non_advancing_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = UUID(int=700)

    class FakeRuns:
        async def submit(self, _prompt: str) -> UUID:
            return run_id

        async def get(self, _run_id: UUID) -> SimpleNamespace:
            return SimpleNamespace(status=RunStatus.RUNNING)

    class FakeWorker:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> bool:
            self.calls += 1
            return True

    worker = FakeWorker()

    @asynccontextmanager
    async def fake_build(**_kwargs: object) -> Any:
        yield SimpleNamespace(
            runs=FakeRuns(),
            worker_factory=lambda _worker_id: worker,
        )

    monkeypatch.setattr(bootstrap_module, "build", fake_build)

    with pytest.raises(RuntimeError, match="did not reach a terminal state within 1 seconds"):
        await _live_execution(
            "balanced",
            (),
            CapabilityBudget(
                model_calls=2,
                tool_calls=0,
                cost_usd=Decimal("0.10"),
                wall_seconds=1,
            ),
            "prompt",
            clock=FixedClock(NOW),
            ids=_ids(4000),
        )

    assert worker.calls == 2


async def test_live_execution_drives_child_run_suspension_to_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = UUID(int=701)

    def run(status: RunStatus) -> SimpleNamespace:
        return SimpleNamespace(
            id=run_id,
            status=status,
            final_message="delegated research complete" if status is RunStatus.COMPLETED else None,
            provider_pin=SimpleNamespace(provider="openai", model="gpt-5.6-sol"),
            usage=SimpleNamespace(
                model_calls=3,
                tool_calls=2,
                cost=Decimal("0.25"),
            ),
            failure=None,
        )

    class FakeRuns:
        def __init__(self) -> None:
            self.states = iter(
                (
                    run(RunStatus.RUNNING),
                    run(RunStatus.WAITING_FOR_APPROVAL),
                    run(RunStatus.QUEUED),
                    run(RunStatus.COMPLETED),
                )
            )

        async def submit(self, _prompt: str) -> UUID:
            return run_id

        async def get(self, _run_id: UUID) -> SimpleNamespace:
            return next(self.states)

        async def events(self, _run_id: UUID) -> list[object]:
            return []

    class FakeApprovals:
        async def list_pending(self, *, run_id: UUID) -> list[object]:
            assert run_id == UUID(int=701)
            return []

    class FakeWorker:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> bool:
            self.calls += 1
            return True

    worker = FakeWorker()

    @asynccontextmanager
    async def fake_build(**_kwargs: object) -> Any:
        yield SimpleNamespace(
            approvals=FakeApprovals(),
            runs=FakeRuns(),
            worker_factory=lambda _worker_id: worker,
        )

    monkeypatch.setattr(bootstrap_module, "build", fake_build)

    execution = await _live_execution(
        "balanced",
        ("delegate.run",),
        CapabilityBudget(
            model_calls=4,
            tool_calls=4,
            cost_usd=Decimal("1.00"),
            wall_seconds=30,
        ),
        "prompt",
        clock=FixedClock(NOW),
        ids=_ids(4100),
    )

    assert execution.status is RunStatus.COMPLETED
    assert execution.output == "delegated research complete"
    assert worker.calls == 3
