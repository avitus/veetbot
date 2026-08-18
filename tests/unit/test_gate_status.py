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
