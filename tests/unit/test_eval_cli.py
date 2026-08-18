import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agent_core.cli.main import app
from agent_core.evals.gates import GateStatus


def test_eval_gates_passes_milestone_and_area_through_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[int, str | None]] = []

    def collect_status(
        _root: object, *, milestone: int, area: str | None = None
    ) -> list[GateStatus]:
        observed.append((milestone, area))
        return [
            GateStatus(
                id="gate.policy.example",
                milestone=3,
                kind="hard",
                check="tests/gates/test_policy_m4.py::test_example",
                outcome="pass",
            )
        ]

    module = SimpleNamespace(
        current_milestone=lambda _root: 10,
        maximum_milestone=lambda _root: 10,
        collect_status=collect_status,
    )
    monkeypatch.setitem(sys.modules, "agent_core.evals.gates", module)

    result = CliRunner().invoke(app, ["eval", "gates", "--milestone", "3", "--area", "policy"])

    assert result.exit_code == 0
    assert observed == [(3, "policy")]
    assert "Milestone 3: 1 gate  1 pass  0 fail  0 pending" in result.stdout


def test_eval_gates_uses_registry_maximum_for_milestone_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = SimpleNamespace(
        current_milestone=lambda _root: 3,
        maximum_milestone=lambda _root: 3,
        collect_status=lambda *_args, **_kwargs: [],
    )
    monkeypatch.setitem(sys.modules, "agent_core.evals.gates", module)

    result = CliRunner().invoke(app, ["eval", "gates", "--milestone", "4"])

    assert result.exit_code == 2
    assert "must not exceed the registry maximum of 3" in result.stderr


def test_eval_gates_normalizes_invalid_registry_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> list[GateStatus]:
        raise ValueError("invalid gate registry: duplicate id")

    module = SimpleNamespace(current_milestone=lambda _root: 9, collect_status=fail)
    monkeypatch.setitem(sys.modules, "agent_core.evals.gates", module)

    result = CliRunner().invoke(app, ["eval", "gates"])

    assert result.exit_code == 1
    assert "gate evaluation failed: invalid gate registry: duplicate id" in result.stderr


def test_eval_gates_returns_failure_when_an_active_gate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = SimpleNamespace(
        current_milestone=lambda _root: 4,
        collect_status=lambda *_args, **_kwargs: [
            GateStatus(
                id="gate.policy.failure",
                milestone=4,
                kind="hard",
                check="tests/gates/test_policy_m4.py::test_failure",
                outcome="fail",
                detail="pytest failure",
            )
        ],
    )
    monkeypatch.setitem(sys.modules, "agent_core.evals.gates", module)

    result = CliRunner().invoke(app, ["eval", "gates"])

    assert result.exit_code == 1
    assert "gate.policy.failure" in result.stdout
    assert "pytest failure" in result.stdout


def test_eval_capability_reports_opt_in_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_live_suite(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.capability",
        SimpleNamespace(run_live_suite=run_live_suite),
    )

    result = CliRunner().invoke(app, ["eval", "capability", "--suite", "research"])

    assert result.exit_code == 0
    assert "skipped: set RUN_LIVE_MODEL_TESTS=1" in result.stdout


def test_eval_capability_normalizes_evaluation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_live_suite(*_args: object, **_kwargs: object) -> None:
        raise ValueError("judge pin mismatch")

    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.capability",
        SimpleNamespace(run_live_suite=run_live_suite),
    )

    result = CliRunner().invoke(app, ["eval", "capability", "--suite", "research"])

    assert result.exit_code == 1
    assert "capability evaluation failed: judge pin mismatch" in result.stderr


def test_eval_capability_returns_failure_for_release_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_live_suite(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            suite="research",
            build_ref="abc123",
            runs=(),
            mean=None,
            floor=None,
            variance=None,
            ceiling_hits=0,
            policy_failures=0,
            release_blocked=True,
            stopped_by="daily_cost_usd",
        )

    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.capability",
        SimpleNamespace(run_live_suite=run_live_suite),
    )

    result = CliRunner().invoke(
        app,
        ["eval", "capability", "--suite", "research", "--build-ref", "abc123"],
    )

    assert result.exit_code == 1
    assert '"release_blocked": true' in result.stdout
    assert '"stopped_by": "daily_cost_usd"' in result.stdout
