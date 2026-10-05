"""The inputs that decide whether CI measures the browser image again (ADR-0152)."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.browser_image_inputs import ROOT, browser_image_inputs

DOCKERFILE = "deploy/browser-profile-service.Dockerfile"
COMPOSE = "deploy/docker-compose.production.yml"
RUNTIME = "src/agent_core/adapters/browser/playwright.py"
CHECK_FILES = (
    "scripts/browser_image_inputs.py",
    "tests/browser_image_probe.py",
    "tests/real_browser_support.py",
    "tests/security/test_browser_image_traffic.py",
)


def _checkout(tmp_path: Path) -> Path:
    for name in (DOCKERFILE, COMPOSE, RUNTIME, "uv.lock", *CHECK_FILES):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_the_inputs_name_the_image_its_limits_playwright_the_switches_and_the_check() -> None:
    inputs = browser_image_inputs()

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    playwright = next(package for package in lock["package"] if package["name"] == "playwright")
    assert set(inputs) == {DOCKERFILE, COMPOSE, "playwright", "launch switches", *CHECK_FILES}
    assert inputs["playwright"] == playwright["version"]


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        (DOCKERFILE, '["en-US"]', '["en-GB"]'),
        (COMPOSE, "mem_limit: 3g", "mem_limit: 4g"),
        (RUNTIME, '"AimEnabled",', '"AimEnabledRenamed",'),
        (RUNTIME, '"--gcm-checkin-url=about:blank"', '"--gcm-checkin-url=about:srcdoc"'),
        ("tests/browser_image_probe.py", "IDLE_SECONDS = 120", "IDLE_SECONDS = 60"),
    ],
)
def test_the_inputs_change_with_what_decides_the_requests(
    tmp_path: Path, name: str, old: str, new: str
) -> None:
    checkout = _checkout(tmp_path)
    before = browser_image_inputs(checkout)

    _edit(checkout / name, old, new)

    assert browser_image_inputs(checkout) != before


def test_the_inputs_change_with_the_playwright_version(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    before = browser_image_inputs(checkout)

    lock = checkout / "uv.lock"
    _edit(
        lock,
        f'name = "playwright"\nversion = "{before["playwright"]}"',
        ('name = "playwright"\nversion = "9.99.0"'),
    )

    assert browser_image_inputs(checkout)["playwright"] == "9.99.0"


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        # The runtime changes often; only its vendor-request switches count.
        (RUNTIME, '"""Launch the isolated browser', '"""Start the isolated browser'),
        # Another locked package leaves the browser build alone.
        ("uv.lock", 'name = "pyyaml"\nversion = "', 'name = "pyyaml"\nversion = "0'),
    ],
)
def test_the_inputs_ignore_the_rest_of_the_runtime_and_the_lock(
    tmp_path: Path, name: str, old: str, new: str
) -> None:
    checkout = _checkout(tmp_path)
    before = browser_image_inputs(checkout)

    _edit(checkout / name, old, new)

    assert browser_image_inputs(checkout) == before


def test_a_renamed_switch_constant_fails_instead_of_dropping_out(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    _edit(checkout / RUNTIME, "_VENDOR_REQUEST_ARGUMENTS = (", "_VENDOR_ARGUMENTS = (")

    with pytest.raises(LookupError, match="_VENDOR_REQUEST_ARGUMENTS"):
        browser_image_inputs(checkout)


def test_ci_reads_the_inputs_with_the_machine_executors_python_alone() -> None:
    """The job reads its inputs before it installs the project, so it can halt cheaply."""

    source = (ROOT / "scripts" / "browser_image_inputs.py").read_text(encoding="utf-8")
    imported = {
        alias.name.partition(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").partition(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.browser_image_inputs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    inputs = browser_image_inputs()
    assert completed.stdout.splitlines() == [f"{name} {inputs[name]}" for name in sorted(inputs)]
