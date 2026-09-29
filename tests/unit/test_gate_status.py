import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_core.evals.gates import (
    _execute_pytest_checks,
    _registry_module,
    collect_status,
    current_milestone,
)


def test_gate_status_executes_active_checks_and_keeps_later_gates_visible() -> None:
    root = Path(__file__).resolve().parents[2]
    executed: list[str] = []

    def execute(_root: Path, checks: Sequence[str]) -> dict[str, tuple[bool, str]]:
        executed.extend(checks)
        return {
            check: (index != 0, "synthetic failure" if index == 0 else "")
            for index, check in enumerate(checks)
        }

    statuses = collect_status(root, milestone=0, area="harness", execute=execute)
    assert executed
    assert any(status.outcome == "fail" for status in statuses)
    assert any(status.outcome == "pending" for status in statuses)
    assert all(status.milestone <= 0 for status in statuses if status.outcome != "pending")


def test_gate_executor_treats_an_active_pytest_skip_as_failure(tmp_path: Path) -> None:
    (tmp_path / "test_gate.py").write_text(
        "import pytest\n\ndef test_gate():\n    pytest.skip('missing prerequisite')\n",
        encoding="utf-8",
    )
    result = _execute_pytest_checks(tmp_path, ["test_gate.py::test_gate"])
    assert result == {"test_gate.py::test_gate": (False, "active gate skipped")}


def test_gate_executor_bounds_the_pytest_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def time_out(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(("pytest",), 1)

    monkeypatch.setattr(subprocess, "run", time_out)

    result = _execute_pytest_checks(tmp_path, ["one", "two"])

    assert result == {
        "one": (False, "pytest gate execution timed out"),
        "two": (False, "pytest gate execution timed out"),
    }


def test_gate_registry_module_is_cached() -> None:
    root = Path(__file__).resolve().parents[2]

    assert _registry_module(root) is _registry_module(root)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("project: [", "cannot parse project-state.yaml"),
        ("project:\n  current_milestone: true\n", "no integer current_milestone"),
    ],
)
def test_current_milestone_rejects_invalid_yaml_values(
    tmp_path: Path, document: str, message: str
) -> None:
    path = tmp_path / "docs" / "status" / "project-state.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        current_milestone(tmp_path)


def test_gate_executor_maps_each_check_to_its_own_pytest_outcome(tmp_path: Path) -> None:
    (tmp_path / "test_gate.py").write_text(
        "import pytest\n\n"
        "def test_pass():\n    assert True\n\n"
        "def test_fail():\n    assert False\n\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_all_cases(value):\n    assert value\n\n"
        "@pytest.mark.parametrize('value', [1, 0])\n"
        "def test_one_case_fails(value):\n    assert value\n\n"
        "@pytest.fixture\n"
        "def broken():\n    raise RuntimeError('setup')\n\n"
        "def test_setup_error(broken):\n    assert True\n",
        encoding="utf-8",
    )
    checks = [
        "test_gate.py::test_pass",
        "test_gate.py::test_fail",
        "test_gate.py::test_all_cases",
        "test_gate.py::test_one_case_fails",
        "test_gate.py::test_setup_error",
    ]

    result = _execute_pytest_checks(tmp_path, checks)

    assert result == {
        "test_gate.py::test_pass": (True, ""),
        "test_gate.py::test_fail": (False, "pytest failure"),
        "test_gate.py::test_all_cases": (True, ""),
        "test_gate.py::test_one_case_fails": (False, "pytest failure"),
        "test_gate.py::test_setup_error": (False, "pytest failure"),
    }
    # A check whose id only extends a real test's id matches nothing that ran.
    assert _execute_pytest_checks(tmp_path, ["test_gate.py::test_pass_twin"]) == {
        "test_gate.py::test_pass_twin": (False, "gate check was not collected")
    }


def test_gate_executor_reports_pytest_output_when_no_report_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_report(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(("pytest",), 4, stdout="", stderr="usage error\n")

    monkeypatch.setattr(subprocess, "run", no_report)

    assert _execute_pytest_checks(tmp_path, ["a", "b"]) == {
        "a": (False, "usage error"),
        "b": (False, "usage error"),
    }


def test_gate_status_fails_an_active_check_the_executor_did_not_report() -> None:
    root = Path(__file__).resolve().parents[2]

    statuses = collect_status(root, milestone=0, area="harness", execute=lambda _r, _c: {})

    active = [status for status in statuses if status.milestone <= 0]
    assert active
    assert {(status.outcome, status.detail) for status in active} == {
        ("fail", "gate check was not executed")
    }
    assert all(
        (status.outcome, status.detail) == ("pending", "")
        for status in statuses
        if status.milestone > 0
    )


def test_gate_status_rejects_an_unknown_area() -> None:
    root = Path(__file__).resolve().parents[2]

    with pytest.raises(ValueError, match="no area 'not-an-area'"):
        collect_status(root, milestone=0, area="not-an-area", execute=lambda _r, _c: {})
