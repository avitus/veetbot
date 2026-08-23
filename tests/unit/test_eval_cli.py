import sys
from pathlib import Path
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


def test_eval_memory_formation_generates_evidence_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[tuple[str, str, str]] = []

    async def run_live_evaluation(
        _root: object,
        *,
        model_policy: str,
        policy_profile: str,
        build_ref: str,
        output: object,
    ) -> SimpleNamespace:
        observed.append((model_policy, policy_profile, build_ref))
        return SimpleNamespace(
            passed=True,
            failure_summary=None,
            model_dump_json=lambda **_kwargs: '{"passed":true}',
        )

    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.memory_formation",
        SimpleNamespace(run_live_evaluation=run_live_evaluation),
    )
    output = str(tmp_path) + "/evidence.json"

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-formation",
            "--model-policy",
            "balanced",
            "--policy-profile",
            "default",
            "--build-ref",
            "abc123",
            "--output",
            output,
        ],
    )

    assert result.exit_code == 0
    assert observed == [("balanced", "default", "abc123")]
    assert '"passed":true' in result.stdout


def test_eval_memory_formation_reports_live_opt_in_skip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def run_live_evaluation(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.memory_formation",
        SimpleNamespace(run_live_evaluation=run_live_evaluation),
    )

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-formation",
            "--model-policy",
            "balanced",
            "--policy-profile",
            "default",
            "--build-ref",
            "abc123",
            "--output",
            str(tmp_path) + "/evidence.json",
        ],
    )

    assert result.exit_code == 0
    assert "skipped: set RUN_LIVE_MODEL_TESTS=1" in result.stdout


def test_eval_memory_formation_returns_structured_failed_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def run_live_evaluation(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            passed=False,
            failure_summary="positive coverage 0/20; policy regression in secret-001",
            model_dump_json=lambda **_kwargs: (
                '{"passed":false,"cases":[{"case_id":"secret-001",'
                '"provider":{"candidate_count":1,"grounded_candidate_count":1}}]}'
            ),
        )

    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.memory_formation",
        SimpleNamespace(run_live_evaluation=run_live_evaluation),
    )

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-formation",
            "--model-policy",
            "balanced",
            "--policy-profile",
            "default",
            "--build-ref",
            "abc123",
            "--output",
            str(tmp_path / "evidence.json"),
        ],
    )

    assert result.exit_code == 1
    assert '"passed":false' in result.stdout
    assert "positive coverage 0/20" in result.stderr


def test_eval_memory_formation_normalizes_failed_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def run_live_evaluation(*_args: object, **_kwargs: object) -> None:
        raise ValueError("provider extraction evaluation observed fabricated candidates")

    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.memory_formation",
        SimpleNamespace(run_live_evaluation=run_live_evaluation),
    )

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-formation",
            "--model-policy",
            "balanced",
            "--policy-profile",
            "default",
            "--build-ref",
            "abc123",
            "--output",
            str(tmp_path) + "/evidence.json",
        ],
    )

    assert result.exit_code == 1
    assert "memory-formation evaluation failed" in result.stderr
    assert "fabricated candidates" in result.stderr


def _benchmark_module(
    observed: list[dict[str, object]],
    *,
    passed: bool = True,
    failure_summary: str | None = None,
    skipped: bool = False,
) -> SimpleNamespace:
    document = f'{{"passed":{str(passed).lower()}}}'

    async def run_benchmark(
        _root: object,
        *,
        deterministic_only: bool,
        model_policy: str,
        policy_profile: str,
        build_ref: str,
        output: Path | None,
        baseline_output: Path | None,
    ) -> SimpleNamespace | None:
        observed.append(
            {
                "deterministic_only": deterministic_only,
                "model_policy": model_policy,
                "policy_profile": policy_profile,
                "build_ref": build_ref,
                "output": output,
                "baseline_output": baseline_output,
            }
        )
        if skipped:
            return None
        return SimpleNamespace(
            passed=passed,
            failure_summary=failure_summary,
            model_dump_json=lambda **_kwargs: document,
        )

    return SimpleNamespace(run_benchmark=run_benchmark)


def _build_ref_module() -> SimpleNamespace:
    return SimpleNamespace(
        resolve_build_ref=lambda _root, explicit: explicit or "resolved-from-git"
    )


def test_eval_memory_benchmark_deterministic_only_prints_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.memory_benchmark_driver",
        _benchmark_module(observed),
    )
    monkeypatch.setitem(sys.modules, "agent_core.evals.capability", _build_ref_module())
    baseline = tmp_path / "baseline.json"

    explicit = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-benchmark",
            "--deterministic-only",
            "--build-ref",
            "abc123",
            "--write-baseline",
            str(baseline),
        ],
    )
    resolved = CliRunner().invoke(app, ["eval", "memory-benchmark"])

    assert explicit.exit_code == 0
    assert '"passed":true' in explicit.stdout
    assert resolved.exit_code == 0
    assert observed == [
        {
            "deterministic_only": True,
            "model_policy": "balanced",
            "policy_profile": "default",
            "build_ref": "abc123",
            "output": None,
            "baseline_output": baseline,
        },
        {
            "deterministic_only": True,
            "model_policy": "balanced",
            "policy_profile": "default",
            "build_ref": "resolved-from-git",
            "output": None,
            "baseline_output": None,
        },
    ]


def test_eval_memory_benchmark_reports_opt_in_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.memory_benchmark_driver",
        _benchmark_module(observed, skipped=True),
    )
    monkeypatch.setitem(sys.modules, "agent_core.evals.capability", _build_ref_module())
    evidence = tmp_path / "evidence.json"

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-benchmark",
            "--no-deterministic-only",
            "--build-ref",
            "abc123",
            "--output",
            str(evidence),
        ],
    )

    assert result.exit_code == 0
    assert "skipped: set RUN_LIVE_MODEL_TESTS=1" in result.stdout
    assert observed[0]["deterministic_only"] is False
    assert observed[0]["output"] == evidence


def test_eval_memory_benchmark_live_requires_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.memory_benchmark_driver",
        _benchmark_module(observed),
    )
    monkeypatch.setitem(sys.modules, "agent_core.evals.capability", _build_ref_module())

    missing_output = CliRunner().invoke(
        app, ["eval", "memory-benchmark", "--no-deterministic-only", "--build-ref", "abc123"]
    )
    missing_build_ref = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-benchmark",
            "--no-deterministic-only",
            "--output",
            str(tmp_path / "evidence.json"),
        ],
    )
    refused_output = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-benchmark",
            "--deterministic-only",
            "--output",
            str(tmp_path / "evidence.json"),
        ],
    )

    assert missing_output.exit_code == 2
    assert "--output is required" in missing_output.stderr
    assert missing_build_ref.exit_code == 2
    assert "--build-ref is required" in missing_build_ref.stderr
    assert refused_output.exit_code == 2
    assert "--output belongs to the live arm" in refused_output.stderr
    assert observed == []


def _external_module(observed: list[dict[str, object]]) -> SimpleNamespace:
    async def run_external_benchmark(
        _root: object,
        *,
        dataset: str,
        path: Path,
        sample: int | None,
        seed: int,
        principal_speaker: str,
        deterministic_only: bool,
        model_policy: str,
        policy_profile: str,
        build_ref: str,
        output: Path,
    ) -> SimpleNamespace:
        observed.append(
            {
                "dataset": dataset,
                "path": path,
                "sample": sample,
                "seed": seed,
                "principal_speaker": principal_speaker,
                "deterministic_only": deterministic_only,
                "build_ref": build_ref,
                "output": output,
            }
        )
        return SimpleNamespace(model_dump_json=lambda **_kwargs: '{"dataset":"locomo"}')

    return SimpleNamespace(run_external_benchmark=run_external_benchmark)


def test_eval_memory_benchmark_external_requires_path_and_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setitem(
        sys.modules, "agent_core.evals.memory_benchmark_external", _external_module(observed)
    )
    monkeypatch.setitem(
        sys.modules, "agent_core.evals.memory_benchmark_driver", _benchmark_module([])
    )
    monkeypatch.setitem(sys.modules, "agent_core.evals.capability", _build_ref_module())
    dataset = tmp_path / "locomo10.json"
    dataset.write_text("[]", encoding="utf-8")
    metrics = tmp_path / "locomo-metrics.json"

    missing_path = CliRunner().invoke(app, ["eval", "memory-benchmark", "--external", "locomo"])
    missing_output = CliRunner().invoke(
        app, ["eval", "memory-benchmark", "--external", "locomo", "--path", str(dataset)]
    )
    unknown = CliRunner().invoke(
        app,
        ["eval", "memory-benchmark", "--external", "nowhere", "--path", str(dataset)],
    )
    stray_path = CliRunner().invoke(app, ["eval", "memory-benchmark", "--path", str(dataset)])
    accepted = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-benchmark",
            "--external",
            "locomo",
            "--path",
            str(dataset),
            "--output",
            str(metrics),
            "--sample",
            "3",
            "--seed",
            "11",
            "--principal-speaker",
            "b",
        ],
    )

    assert missing_path.exit_code == 2
    assert "--path is required" in missing_path.stderr
    assert missing_output.exit_code == 2
    assert "--output is required" in missing_output.stderr
    assert unknown.exit_code == 2
    assert "unknown dataset" in unknown.stderr
    assert stray_path.exit_code == 2
    assert "--path belongs to an external dataset run" in stray_path.stderr
    assert accepted.exit_code == 0
    assert '"dataset":"locomo"' in accepted.stdout
    assert observed == [
        {
            "dataset": "locomo",
            "path": dataset,
            "sample": 3,
            "seed": 11,
            "principal_speaker": "b",
            "deterministic_only": True,
            "build_ref": "resolved-from-git",
            "output": metrics,
        }
    ]


def test_eval_memory_benchmark_returns_failure_for_a_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "agent_core.evals.memory_benchmark_driver",
        _benchmark_module([], passed=False, failure_summary="needed_recalled regressed"),
    )
    monkeypatch.setitem(sys.modules, "agent_core.evals.capability", _build_ref_module())

    result = CliRunner().invoke(app, ["eval", "memory-benchmark", "--build-ref", "abc123"])

    assert result.exit_code == 1
    assert '"passed":false' in result.stdout
    assert "needed_recalled regressed" in result.stderr
