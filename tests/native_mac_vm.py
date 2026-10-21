"""Run the native gates' macOS XCTest journeys inside the local Tart VM.

The macOS UI cases synthesize real pointer and keyboard input, so on the host
they take over the owner's desktop (docs/plan/development-toolchain.md, job 6).
The native gates send only their macOS `xcodebuild test` through
`scripts/run_apple_ui_macos_vm.sh`; package tests and simulator destinations
stay on the host. A machine without the VM cannot verify the macOS journeys, so
the gate skips, which the registry counts as unpassed. Setting
VEETBOT_NATIVE_MAC_ON_HOST=1 runs them on the host deliberately.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VM_RUNNER = ROOT / "scripts" / "run_apple_ui_macos_vm.sh"
HOST_OVERRIDE = "VEETBOT_NATIVE_MAC_ON_HOST"
# A run may wait for another session's use of the VM before its own boot.
VM_QUEUE_ALLOWANCE = 1800


def macos_journeys_unavailable(environment: dict[str, str]) -> str | None:
    """Why the macOS journeys cannot run here, or None when they can."""
    if environment.get(HOST_OVERRIDE) == "1":
        return None
    tart = shutil.which("tart", path=environment.get("PATH"))
    if tart is None:
        return (
            "Tart is not installed, so the macOS journeys cannot run in the VM; "
            "see docs/apple-client.md. This is not a passing gate."
        )
    vm = environment.get("APPLE_UI_VM", "veetbot-macos-ui")
    listing = subprocess.run(
        [tart, "list", "--format", "json"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    names = {entry["Name"] for entry in json.loads(listing.stdout or "[]")}
    if listing.returncode != 0 or vm not in names:
        return (
            f"Tart VM {vm!r} does not exist, so the macOS journeys cannot run; "
            "see docs/apple-client.md. This is not a passing gate."
        )
    return None


def run_macos_journeys(
    cases: list[str], bundle: Path, environment: dict[str, str], *, timeout: int = 1200
) -> str:
    """Run the named macOS UI cases and leave their result bundle at `bundle`."""
    arguments = [
        "test",
        "-quiet",
        "-project",
        "clients/apple/Veetbot.xcodeproj",
        "-scheme",
        "Veetbot",
        "-destination",
        "platform=macOS",
        *[f"-only-testing:VeetbotUITests/ConversationNavigationUITests/{case}" for case in cases],
        "CODE_SIGN_STYLE=Manual",
        "CODE_SIGN_IDENTITY=-",
        "CODE_SIGNING_REQUIRED=NO",
        "CODE_SIGN_ENTITLEMENTS=",
        "PROVISIONING_PROFILE_SPECIFIER=",
        "DEVELOPMENT_TEAM=",
    ]
    if environment.get(HOST_OVERRIDE) == "1":
        command = ["xcodebuild", *arguments, "-resultBundlePath", str(bundle)]
        run_environment = environment
    else:
        # The guest writes into the runner's results share, a fresh
        # `vm-run-*` directory under APPLE_TEST_RESULTS_DIR.
        results_root = bundle.parent / f"{bundle.stem}-vm"
        run_environment = {**environment, "APPLE_TEST_RESULTS_DIR": str(results_root)}
        command = [
            str(VM_RUNNER),
            "/bin/zsh",
            "-c",
            'exec xcodebuild "$@" -resultBundlePath "$APPLE_TEST_RESULTS_DIR/macos.xcresult"',
            "zsh",
            *arguments,
        ]
        timeout += VM_QUEUE_ALLOWANCE
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=run_environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-16000:]
    if command[0] != "xcodebuild":
        produced = sorted(results_root.glob("vm-run-*/macos.xcresult"))
        assert len(produced) == 1, (
            f"Expected one VM result bundle, found {produced}\n{output[-4000:]}"
        )
        shutil.move(produced[0], bundle)
    return output
