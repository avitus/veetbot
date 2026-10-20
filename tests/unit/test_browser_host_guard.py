"""Real Chromium never opens on the owner's Mac desktop (ADR-0229)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest
from tests.browser_host import HOST_OVERRIDE, host_browser_refusal

ROOT = Path(__file__).resolve().parents[2]


def test_a_mac_host_refuses_real_browser_cases_and_names_the_sidecar_route() -> None:
    reason = host_browser_refusal("darwin", {})

    assert reason is not None
    assert "make test-browser" in reason
    assert HOST_OVERRIDE in reason


def test_linux_and_a_deliberate_override_run_real_browser_cases() -> None:
    assert host_browser_refusal("linux", {}) is None
    assert host_browser_refusal("darwin", {HOST_OVERRIDE: "1"}) is None


def test_suite_setup_skips_a_browser_case_on_a_mac_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv(HOST_OVERRIDE, raising=False)
    browser_case = SimpleNamespace(
        get_closest_marker=lambda name: object() if name == "browser" else None
    )

    with pytest.raises(pytest.skip.Exception, match="sidecar"):
        conftest.pytest_runtest_setup(browser_case)  # type: ignore[arg-type]


def test_suite_setup_leaves_other_cases_alone_on_a_mac_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv(HOST_OVERRIDE, raising=False)
    static_case = SimpleNamespace(
        get_closest_marker=lambda name: object() if name == "static" else None
    )

    conftest.pytest_runtest_setup(static_case)  # type: ignore[arg-type]


def _dry_run(*arguments: str) -> str:
    return subprocess.run(
        ["make", "--no-print-directory", "-n", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout


@pytest.mark.parametrize("target", ["test-browser", "test-browser-cases"])
def test_a_mac_sends_the_browser_targets_to_the_sidecar(target: str) -> None:
    commands = _dry_run(target, "HOST_OS=Darwin", "CASES=tests/unit/test_browser_rich_text.py")

    assert "scripts/run_on_sidecar.sh" in commands
    assert "playwright install" not in commands
    assert "pytest" not in commands.replace("scripts/run_on_sidecar.sh", "")


def test_linux_runs_the_browser_partition_in_place() -> None:
    commands = _dry_run("test-browser", "HOST_OS=Linux")

    assert "scripts/run_on_sidecar.sh" not in commands
    assert "playwright install --with-deps chromium" in commands
    assert "pytest -n 0 -m browser" in commands


def test_linux_runs_named_browser_cases_without_writing_the_gate_report() -> None:
    commands = _dry_run(
        "test-browser-cases", "HOST_OS=Linux", "CASES=tests/unit/test_browser_rich_text.py"
    )

    assert "VEETBOT_REQUIRE_REAL_BROWSER=1" in commands
    assert "tests/unit/test_browser_rich_text.py" in commands
    assert "--browser-report" not in commands
