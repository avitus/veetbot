"""The native gates' macOS journeys run in the Tart VM, never on the owner's desktop."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests import native_mac_vm

ROOT = Path(__file__).resolve().parents[2]


def test_no_native_gate_names_a_macos_destination_itself() -> None:
    """Only the VM helper builds a macOS xcodebuild destination."""
    for module in sorted((ROOT / "tests" / "native").glob("*.py")):
        assert "platform=macOS" not in module.read_text(encoding="utf-8"), module.name


def test_macos_journeys_run_through_the_vm_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        calls.append((command, environment))
        produced = Path(environment["APPLE_TEST_RESULTS_DIR"]) / "vm-run-1" / "macos.xcresult"
        produced.mkdir(parents=True)
        (produced / "Info.plist").write_text("bundle", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    bundle = tmp_path / "macos.xcresult"

    native_mac_vm.run_macos_journeys(["testOne", "testTwo"], bundle, {"PATH": "/usr/bin"})

    [(command, environment)] = calls
    assert command[0] == str(ROOT / "scripts" / "run_apple_ui_macos_vm.sh")
    assert any(part.startswith("exec xcodebuild ") for part in command)
    assert "platform=macOS" in command
    assert "-only-testing:VeetbotUITests/ConversationNavigationUITests/testTwo" in command
    assert Path(environment["APPLE_TEST_RESULTS_DIR"]).is_relative_to(tmp_path)
    assert (bundle / "Info.plist").read_text(encoding="utf-8") == "bundle"


def test_a_failed_vm_run_fails_the_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 65, "", "** TEST FAILED **")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AssertionError, match="TEST FAILED"):
        native_mac_vm.run_macos_journeys(["testOne"], tmp_path / "macos.xcresult", {})


def test_a_missing_vm_is_reported_so_the_gate_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)
    reason = native_mac_vm.macos_journeys_unavailable({"PATH": "/usr/bin"})
    assert reason is not None and "Tart" in reason


def test_the_host_override_runs_on_the_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    environment = {native_mac_vm.HOST_OVERRIDE: "1"}
    assert native_mac_vm.macos_journeys_unavailable(environment) is None

    bundle = tmp_path / "macos.xcresult"
    native_mac_vm.run_macos_journeys(["testOne"], bundle, environment)

    [command] = calls
    assert command[0] == "xcodebuild"
    assert command[command.index("-resultBundlePath") + 1] == str(bundle)
