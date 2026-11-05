"""Real Chromium never opens on the owner's Mac desktop (ADR-0236)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest
from tests.browser_host import HOST_OVERRIDE, ROUTE_SETTING, host_browser_refusal

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


def _dry_run(*arguments: str, check: bool = True) -> str:
    environment = dict(os.environ)
    for variable in (
        "MAKEFLAGS",
        "MFLAGS",
        "MAKELEVEL",
        HOST_OVERRIDE,
        ROUTE_SETTING,
        "BROWSER_TEST_WORKERS",
        "CONTAINER_BROWSER_TEST_WORKERS",
    ):
        environment.pop(variable, None)
    return subprocess.run(
        ["make", "--no-print-directory", "-n", *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=check,
    ).stdout


@pytest.mark.parametrize("target", ["test-browser", "test-browser-cases"])
def test_a_mac_sends_the_browser_targets_to_the_sidecar_when_asked(target: str) -> None:
    commands = _dry_run(
        target,
        "HOST_OS=Darwin",
        f"{ROUTE_SETTING}=sidecar",
        "CASES=tests/unit/test_browser_rich_text.py",
    )

    assert "scripts/run_on_sidecar.sh" in commands
    assert "playwright install" not in commands
    assert "pytest" not in commands.replace("scripts/run_on_sidecar.sh", "")


@pytest.mark.parametrize("target", ["test-browser", "test-browser-cases"])
def test_a_mac_can_send_the_browser_targets_to_a_local_linux_container(target: str) -> None:
    commands = _dry_run(
        target,
        "HOST_OS=Darwin",
        f"{ROUTE_SETTING}=docker",
        "CASES=tests/unit/test_browser_rich_text.py",
    )

    assert "scripts/run_in_linux_container.sh" in commands
    assert "scripts/run_on_sidecar.sh" not in commands
    assert "playwright install" not in commands
    assert "pytest" not in commands.replace("scripts/run_in_linux_container.sh", "")


def test_an_unknown_browser_route_fails_instead_of_opening_windows() -> None:
    environment = dict(os.environ)
    environment.pop(HOST_OVERRIDE, None)
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-n",
            "test-browser",
            "HOST_OS=Darwin",
            f"{ROUTE_SETTING}=desktop",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert ROUTE_SETTING in result.stderr


def test_the_local_container_is_linux_x86_64_with_a_private_display() -> None:
    script = (ROOT / "scripts" / "run_in_linux_container.sh").read_text(encoding="utf-8")
    image = (ROOT / "deploy" / "browser-test.Dockerfile").read_text(encoding="utf-8")

    # x86_64 matches the sidecar and hosted CI; arm64 Chromium differs (ADR-0236).
    assert script.count("--platform linux/amd64") == 2
    assert "HOST_OS=Linux" in script
    assert "--shm-size" in script
    assert "uv.lock" in script
    assert "playwright install --with-deps chromium" in image
    assert "xvfb" in image
    assert "uv==0.8.6" in image


def test_browser_dry_run_is_independent_of_parent_make_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HOST_OVERRIDE, "1")
    monkeypatch.setenv("MAKEFLAGS", "-s")
    monkeypatch.setenv("MFLAGS", "-s")
    monkeypatch.setenv("MAKELEVEL", "4")
    monkeypatch.setenv("BROWSER_TEST_WORKERS", "3")
    monkeypatch.setenv("CONTAINER_BROWSER_TEST_WORKERS", "5")

    commands = _dry_run("test-browser", "HOST_OS=Darwin")

    assert "scripts/run_in_linux_container.sh" in commands
    assert "BROWSER_TEST_WORKERS=8" in commands
    assert "playwright install" not in commands


def test_a_mac_defaults_to_the_local_container_with_parallel_workers() -> None:
    commands = _dry_run("test-browser", "HOST_OS=Darwin")

    assert "scripts/run_in_linux_container.sh" in commands
    assert "BROWSER_TEST_WORKERS=8" in commands


def test_linux_runs_the_browser_partition_in_place() -> None:
    commands = _dry_run("test-browser", "HOST_OS=Linux")

    assert "scripts/run_on_sidecar.sh" not in commands
    assert "playwright install --with-deps chromium" in commands
    assert "pytest -n 0 --dist loadfile -m browser" in commands


def test_linux_can_spread_the_browser_partition_over_workers() -> None:
    commands = _dry_run("test-browser", "HOST_OS=Linux", "BROWSER_TEST_WORKERS=8")

    assert "pytest -n 8 --dist loadfile -m browser" in commands
    assert "--browser-report" in commands


def test_linux_runs_named_browser_cases_without_writing_the_gate_report() -> None:
    commands = _dry_run(
        "test-browser-cases", "HOST_OS=Linux", "CASES=tests/unit/test_browser_rich_text.py"
    )

    assert "VEETBOT_REQUIRE_REAL_BROWSER=1" in commands
    assert "tests/unit/test_browser_rich_text.py" in commands
    assert "--browser-report" not in commands
