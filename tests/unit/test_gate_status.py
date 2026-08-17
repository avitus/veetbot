from collections.abc import Sequence
from pathlib import Path

from agent_core.evals.gates import _execute_pytest_checks, collect_status


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
