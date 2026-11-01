"""ADR-0281: the entry point behind code.test, run as the real subprocess it is in the sandbox."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "src/agent_core/execution/code_checks.py"
REPORT_KEYS = {"passed", "exit_code", "timed_out", "output", "seconds"}

PASSING = "def test_passes():\n    assert True\n"
FAILING = "def test_fails_for_a_named_reason():\n    assert 1 == 2\n"


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    _write(
        root,
        {
            "tests/unit/test_ok.py": PASSING,
            "tests/unit/test_other.py": "def test_other():\n    assert True\n",
            "tests/native/test_native.py": "def test_native():\n    raise SystemExit(99)\n",
            "src/sample/__init__.py": "",
            "src/sample/value.py": "VALUE = 1\n",
            "docs/note.md": "A document.\n",
        },
    )
    return root


def _check(
    source: Path,
    tmp_path: Path,
    check: dict[str, Any],
    *,
    files: dict[str, str | None] | None = None,
    timeout_seconds: int | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    arguments = [sys.executable, "-I", str(ENTRY), "--source", str(source)]
    if timeout_seconds is not None:
        arguments += ["--timeout-seconds", str(timeout_seconds)]
    result = subprocess.run(
        arguments,
        input=json.dumps({"files": files or {}, "check": check}),
        text=True,
        capture_output=True,
        check=False,
        cwd=workspace,
        env={"PATH": os.environ["PATH"], **(environment or {})},
        timeout=120,
    )
    assert result.stderr == ""
    return json.loads(result.stdout), result.returncode, workspace


def test_a_passing_test_reports_passed_and_every_report_field(source: Path, tmp_path: Path) -> None:
    report, exit_code, _ = _check(
        source, tmp_path, {"kind": "pytest", "targets": ["tests/unit/test_ok.py"]}
    )
    assert set(report) == REPORT_KEYS
    assert report["passed"] is True
    assert report["exit_code"] == 0
    assert report["timed_out"] is False
    assert "1 passed" in report["output"]
    assert report["seconds"] >= 0
    assert exit_code == 0


def test_a_failing_test_is_reported_as_not_passed(source: Path, tmp_path: Path) -> None:
    _write(source, {"tests/unit/test_bad.py": FAILING})
    report, exit_code, _ = _check(
        source, tmp_path, {"kind": "pytest", "targets": ["tests/unit/test_bad.py"]}
    )
    assert report["passed"] is False
    assert report["exit_code"] == 1
    assert "test_fails_for_a_named_reason" in report["output"]
    assert exit_code == 1


def test_a_run_that_collects_nothing_is_not_a_pass(source: Path, tmp_path: Path) -> None:
    report, exit_code, _ = _check(
        source, tmp_path, {"kind": "pytest", "targets": ["tests/unit"], "keyword": "nomatch"}
    )
    assert report["passed"] is False
    assert report["exit_code"] == 5
    assert exit_code == 1


def test_pytest_defaults_to_the_unit_tests_and_honours_the_keyword(
    source: Path, tmp_path: Path
) -> None:
    report, _, _ = _check(source, tmp_path, {"kind": "pytest"})
    assert report["passed"] is True
    assert "2 passed" in report["output"]
    report, _, _ = _check(source, tmp_path, {"kind": "pytest", "keyword": "test_other"})
    assert "1 passed" in report["output"]


def test_native_tests_never_run_even_from_the_parent_directory(
    source: Path, tmp_path: Path
) -> None:
    report, _, _ = _check(source, tmp_path, {"kind": "pytest", "targets": ["tests"]})
    assert report["passed"] is True
    assert "2 passed" in report["output"]


@pytest.mark.parametrize(
    "target",
    [
        "/etc/passwd",
        "../outside.py",
        "tests/../../outside.py",
        "tests/unit/../../docs/note.md",
        "-p",
        "--help",
        "--collect-only",
        "docs/note.md",
        "pyproject.toml",
        "tests/unit/missing_test.py",
        "tests/native/test_native.py",
        "tests/native",
        "tests/unit/test_ok.py::test_passes",
        "tests//unit",
        "./tests/unit",
        "",
        7,
    ],
)
def test_a_refused_target_runs_nothing(source: Path, tmp_path: Path, target: object) -> None:
    report, exit_code, _ = _check(source, tmp_path, {"kind": "pytest", "targets": [target]})
    assert set(report) == REPORT_KEYS
    assert report["passed"] is False
    assert report["exit_code"] is None
    assert "Refused" in report["output"]
    assert "passed" not in report["output"]
    assert exit_code == 1


def test_a_target_through_a_symbolic_link_is_refused(source: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, {"test_outside.py": PASSING})
    (source / "tests/unit/linked").symlink_to(outside)
    report, _, _ = _check(
        source, tmp_path, {"kind": "pytest", "targets": ["tests/unit/linked/test_outside.py"]}
    )
    assert report["passed"] is False
    assert "Refused" in report["output"]


@pytest.mark.parametrize(
    "check",
    [
        {"kind": "shell", "targets": ["tests/unit"]},
        {"kind": "pytest", "keyword": "a or b"},
        {"kind": "pytest", "keyword": "-x"},
        {"kind": "ruff", "keyword": "anything"},
        {"kind": "pytest", "arguments": ["--help"]},
        {"kind": "pytest", "targets": "tests/unit"},
    ],
)
def test_a_check_outside_the_closed_set_is_refused(
    source: Path, tmp_path: Path, check: dict[str, Any]
) -> None:
    report, exit_code, _ = _check(source, tmp_path, check)
    assert report["passed"] is False
    assert report["exit_code"] is None
    assert "Refused" in report["output"]
    assert exit_code == 1


def test_unreadable_input_is_reported_rather_than_crashing(source: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", str(ENTRY), "--source", str(source)],
        input="not json",
        text=True,
        capture_output=True,
        check=False,
        cwd=workspace,
    )
    report = json.loads(result.stdout)
    assert report["passed"] is False
    assert report["exit_code"] is None
    assert result.returncode == 1


def test_the_draft_replaces_the_baked_file_and_imports_resolve_to_the_workspace(
    source: Path, tmp_path: Path
) -> None:
    _write(
        source,
        {
            "tests/unit/test_value.py": (
                "from sample.value import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n"
            )
        },
    )
    target = {"kind": "pytest", "targets": ["tests/unit/test_value.py"]}
    report, _, _ = _check(source, tmp_path, target)
    assert report["passed"] is False
    (tmp_path / "workspace").rename(tmp_path / "first-workspace")
    report, _, workspace = _check(
        source, tmp_path, target, files={"src/sample/value.py": "VALUE = 2\n"}
    )
    assert report["passed"] is True, report["output"]
    assert (workspace / "src/sample/value.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (source / "src/sample/value.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_the_draft_adds_and_deletes_files(source: Path, tmp_path: Path) -> None:
    report, _, workspace = _check(
        source,
        tmp_path,
        {"kind": "pytest"},
        files={
            "tests/unit/deep/er/test_new.py": "def test_new():\n    assert True\n",
            "tests/unit/test_other.py": None,
            "tests/unit/never_existed.py": None,
        },
    )
    assert report["passed"] is True, report["output"]
    assert "2 passed" in report["output"]
    assert not (workspace / "tests/unit/test_other.py").exists()
    assert (source / "tests/unit/test_other.py").exists()


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/escaped.py",
        "../escaped.py",
        "tests/../../escaped.py",
        "-escaped.py",
        "tests/unit/-p",
        "docs/note.md",
        "deploy/release.sh",
        ".circleci/config.yml",
        "tests",
        "tests/unit/",
        "tests//unit/x.py",
        "tests/unit/nul\x00.py",
        "",
    ],
)
def test_a_refused_file_path_writes_nothing(source: Path, tmp_path: Path, path: str) -> None:
    report, exit_code, workspace = _check(
        source, tmp_path, {"kind": "pytest"}, files={path: "ESCAPED = True\n"}
    )
    assert report["passed"] is False
    assert report["exit_code"] is None
    assert "Refused" in report["output"]
    assert exit_code == 1
    # Refused before the baked tree was copied, so nothing landed anywhere.
    assert list(workspace.iterdir()) == []
    assert not (tmp_path / "escaped.py").exists()


def test_a_write_or_delete_never_follows_a_symbolic_link(source: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, {"kept.py": "KEPT = True\n"})
    (source / "tests/unit/linked_directory").symlink_to(outside)
    (source / "tests/unit/linked_file.py").symlink_to(outside / "kept.py")
    for path, content in (
        ("tests/unit/linked_directory/kept.py", "OVERWRITTEN = True\n"),
        ("tests/unit/linked_directory/new.py", "OVERWRITTEN = True\n"),
        ("tests/unit/linked_directory/kept.py", None),
        ("tests/unit/linked_file.py", "OVERWRITTEN = True\n"),
        ("tests/unit/linked_file.py", None),
    ):
        report, _, workspace = _check(source, tmp_path, {"kind": "pytest"}, files={path: content})
        assert report["passed"] is False, path
        assert "Refused" in report["output"], path
        assert (outside / "kept.py").read_text(encoding="utf-8") == "KEPT = True\n"
        assert sorted(item.name for item in outside.iterdir()) == ["kept.py"]
        shutil.rmtree(workspace)


def test_deleting_a_directory_is_refused(source: Path, tmp_path: Path) -> None:
    report, _, workspace = _check(source, tmp_path, {"kind": "pytest"}, files={"tests/unit": None})
    assert report["passed"] is False
    assert "Refused" in report["output"]
    assert (workspace / "tests/unit/test_ok.py").exists()


def test_the_check_inherits_no_environment(source: Path, tmp_path: Path) -> None:
    _write(
        source,
        {
            "tests/unit/test_environment.py": (
                "import os\n\n\ndef test_clean():\n"
                "    assert 'SYNTHETIC_PARENT_VALUE' not in os.environ\n"
            )
        },
    )
    report, _, _ = _check(
        source,
        tmp_path,
        {"kind": "pytest", "targets": ["tests/unit/test_environment.py"]},
        environment={"SYNTHETIC_PARENT_VALUE": "must-not-reach-the-check"},
    )
    assert report["passed"] is True, report["output"]


def test_the_output_is_the_bounded_tail(source: Path, tmp_path: Path) -> None:
    _write(
        source,
        {
            "tests/unit/test_loud.py": (
                "def test_loud():\n"
                "    print('early-marker')\n"
                "    print('x' * 200_000)\n"
                "    print('late-marker')\n"
                "    assert False\n"
            )
        },
    )
    report, _, _ = _check(source, tmp_path, {"kind": "pytest", "targets": ["tests/unit"]})
    assert len(report["output"]) <= 6000
    assert "early-marker" not in report["output"]
    assert "failed" in report["output"]


def test_a_run_past_the_deadline_is_killed_and_reported_as_timed_out(
    source: Path, tmp_path: Path
) -> None:
    _write(
        source,
        {
            "tests/unit/test_slow.py": (
                "import time\n\n\ndef test_slow():\n    print('started', flush=True)\n"
                "    time.sleep(60)\n"
            )
        },
    )
    report, exit_code, _ = _check(
        source,
        tmp_path,
        {"kind": "pytest", "targets": ["tests/unit/test_slow.py"]},
        timeout_seconds=3,
    )
    assert report["timed_out"] is True
    assert report["passed"] is False
    assert report["exit_code"] is None
    assert report["seconds"] < 30
    assert exit_code == 1


def test_ruff_reports_a_finding_and_passes_clean_code(source: Path, tmp_path: Path) -> None:
    _write(source, {"src/sample/unused.py": "import os\n"})
    target = {"kind": "ruff", "targets": ["src/sample/unused.py"]}
    report, _, _ = _check(source, tmp_path, target)
    assert report["passed"] is False
    assert report["exit_code"] == 1
    assert "F401" in report["output"]
    (tmp_path / "workspace").rename(tmp_path / "first-workspace")
    report, _, workspace = _check(source, tmp_path, target, files={"src/sample/unused.py": "\n"})
    assert report["passed"] is True, report["output"]
    assert not (workspace / ".ruff_cache").exists()


def test_mypy_reports_a_type_error_and_writes_no_cache(source: Path, tmp_path: Path) -> None:
    _write(source, {"src/sample/typed.py": "VALUE: int = 'text'\n"})
    report, _, workspace = _check(
        source, tmp_path, {"kind": "mypy", "targets": ["src/sample/typed.py"]}
    )
    assert report["passed"] is False
    assert "Incompatible types" in report["output"]
    assert not (workspace / ".mypy_cache").exists()


def _cockpit(source: Path) -> None:
    _write(
        source,
        {
            "cockpit/scripts/build-runtime.mjs": (
                "import { mkdirSync, writeFileSync } from 'node:fs';\n"
                "mkdirSync('dist', { recursive: true });\n"
                "writeFileSync('dist/built.txt', 'built');\n"
            ),
            "cockpit/tests/plain.test.mjs": (
                "import test from 'node:test';\n"
                "import assert from 'node:assert/strict';\n"
                "import { readFileSync } from 'node:fs';\n"
                "test('plain', () => {\n"
                "  assert.equal(readFileSync('dist/built.txt', 'utf8'), 'built');\n"
                "});\n"
            ),
            "cockpit/tests/realm.test.mjs": "throw new Error('needs a browser engine');\n",
            "cockpit/tests/builder.test.mjs": "throw new Error('needs a browser engine');\n",
        },
    )


def test_cockpit_builds_then_runs_only_the_tests_that_need_no_browser(
    source: Path, tmp_path: Path
) -> None:
    _cockpit(source)
    report, exit_code, _ = _check(source, tmp_path, {"kind": "cockpit"})
    assert report["passed"] is True, report["output"]
    assert exit_code == 0


def test_cockpit_reports_a_failing_test(source: Path, tmp_path: Path) -> None:
    _cockpit(source)
    report, exit_code, _ = _check(
        source,
        tmp_path,
        {"kind": "cockpit", "targets": ["cockpit/tests/plain.test.mjs"]},
        files={
            "cockpit/tests/plain.test.mjs": (
                "import test from 'node:test';\n"
                "test('broken', () => { throw new Error('broken on purpose'); });\n"
            )
        },
    )
    assert report["passed"] is False
    assert "broken on purpose" in report["output"]
    assert exit_code == 1


@pytest.mark.parametrize(
    "target",
    ["cockpit/tests/realm.test.mjs", "cockpit/tests/builder.test.mjs", "cockpit/scripts"],
)
def test_cockpit_refuses_a_browser_test_or_a_path_that_is_no_test(
    source: Path, tmp_path: Path, target: str
) -> None:
    _cockpit(source)
    report, _, _ = _check(source, tmp_path, {"kind": "cockpit", "targets": [target]})
    assert report["passed"] is False
    assert report["exit_code"] is None
    assert "Refused" in report["output"]


def test_the_entry_point_imports_only_the_standard_library() -> None:
    tree = ENTRY.read_text(encoding="utf-8")
    assert "agent_core" not in "\n".join(
        line for line in tree.splitlines() if line.startswith(("import ", "from "))
    )
    # Runs in isolated mode, where nothing but the standard library can resolve.
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(ENTRY), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
