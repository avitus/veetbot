"""Execute the Make graph with harmless commands to test scheduling and coverage."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    shutil.copyfile(ROOT / "Makefile", tmp_path / "Makefile")
    stub = tmp_path / "command"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "root = pathlib.Path.cwd()\n"
        "args = sys.argv[1:]\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "with (root / 'commands.jsonl').open('a') as log:\n"
        "    log.write(json.dumps([name, *args]) + '\\n')\n"
        "if 'static' in args:\n"
        "    (root / 'static.started').touch()\n"
        "    if os.environ.get('EXPECT_OVERLAP'):\n"
        "        deadline = time.monotonic() + 3\n"
        "        while not (root / 'website.started').exists():\n"
        "            if time.monotonic() > deadline: sys.exit(42)\n"
        "            time.sleep(0.01)\n"
        "    (root / 'static.finished').touch()\n"
        "if any('not static' in arg for arg in args):\n"
        "    if os.environ.get('EXPECT_STATIC_FIRST'):\n"
        "        assert (root / 'static.finished').exists()\n"
        "if name in ('npm', 'node'): (root / 'website.started').touch()\n"
        "if os.environ.get('FAIL_STAGE') in args: sys.exit(17)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    for name in ("uv", "npm", "node"):
        (tmp_path / name).symlink_to(stub)
    for relative in (
        "deploy/app/release.test.sh",
        "deploy/app/rollback.test.sh",
        "deploy/nginx/deploy.test.sh",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(stub)
    return tmp_path


def run_make(checkout: Path, *goals: str, **settings: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{checkout}{os.pathsep}{os.environ['PATH']}", **settings}
    # A subprocess is a fresh invocation, not a participant in pytest's parent Make jobserver.
    env.pop("MAKEFLAGS", None)
    env.pop("MFLAGS", None)
    env.pop("MAKELEVEL", None)
    return subprocess.run(
        ["make", *goals], cwd=checkout, env=env, capture_output=True, text=True, timeout=15
    )


def commands(checkout: Path) -> list[list[str]]:
    return [json.loads(line) for line in (checkout / "commands.jsonl").read_text().splitlines()]


def test_check_overlaps_independent_work_and_orders_python_partitions(checkout: Path) -> None:
    result = run_make(checkout, "check", EXPECT_OVERLAP="1", EXPECT_STATIC_FIRST="1")
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_runs_each_required_command_once_even_with_redundant_goals(checkout: Path) -> None:
    result = run_make(checkout, "check", "docs-check", "test-static", "test-fast")
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = commands(checkout)
    for selector in (
        "static",
        "not static and not integration and not live and not browser",
        "browser",
    ):
        assert sum(selector in command for command in recorded) == 1
    for token in (
        "mypy",
        "scripts/check_docs.py",
        "release.test.sh",
        "rollback.test.sh",
        "deploy.test.sh",
    ):
        assert sum(any(token in arg for arg in command) for command in recorded) == 1
    assert sum("ruff" in command for command in recorded) == 2
    assert sum(command == ["npm", "--prefix", "website", "test"] for command in recorded) == 1
    assert (
        sum(command == ["npm", "--prefix", "website", "run", "lint"] for command in recorded) == 1
    )


def test_static_uses_two_workers_with_a_serial_override(checkout: Path) -> None:
    result = run_make(checkout, "test-static")
    assert result.returncode == 0, result.stderr
    command = commands(checkout)[0]
    assert command[command.index("-n") + 1] == "2"
    assert command[command.index("--dist") + 1] == "loadscope"
    result = run_make(checkout, "test-static", "STATIC_TEST_WORKERS=0")
    assert result.returncode == 0, result.stderr
    command = commands(checkout)[-1]
    assert command[command.index("-n") + 1] == "0"


@pytest.mark.parametrize(
    "parent_options", ["", "-n 2 --dist loadscope --junitxml=parent-results.xml"]
)
def test_general_test_target_excludes_live_and_browser_image(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, parent_options: str
) -> None:
    """The ordinary Make target must not run dedicated credential/image lanes."""
    monkeypatch.setenv("PYTEST_ADDOPTS", parent_options)
    result = run_make(checkout, "test")
    assert result.returncode == 0, result.stderr
    command = commands(checkout)[0]
    marker = command[command.index("-m") + 1]
    suite = checkout / "test_selection.py"
    suite.write_text(
        "import pytest\n"
        "def test_ordinary(): pass\n"
        "@pytest.mark.live\n"
        "def test_live(): raise AssertionError('live lane selected')\n"
        "@pytest.mark.browser_image\n"
        "def test_image(): raise AssertionError('image lane selected')\n",
        encoding="utf-8",
    )
    selected = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-c", "/dev/null", "-m", marker, str(suite)],
        cwd=checkout,
        # This isolated suite owns its selection, workers, and result artifacts.
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert "1 passed, 2 deselected" in selected.stdout


def test_contract_alone_does_not_rerun_static_tests(checkout: Path) -> None:
    result = run_make(checkout, "test-contract")
    assert result.returncode == 0, result.stderr
    assert len(commands(checkout)) == 1


@pytest.mark.parametrize("stage", ["mypy", "static", "browser", "scripts/check_docs.py", "lint"])
def test_parallel_check_propagates_failures(checkout: Path, stage: str) -> None:
    result = run_make(checkout, "check", FAIL_STAGE=stage)
    assert result.returncode != 0


def test_check_can_run_serially(checkout: Path) -> None:
    result = run_make(checkout, "check", "CHECK_JOBS=1", EXPECT_STATIC_FIRST="1")
    assert result.returncode == 0, result.stdout + result.stderr
