"""Execute native Email acceptance checks; unavailable native lanes never pass a gate.

These checks run the real Swift tests and XCTest app on the local Apple toolchain.
They do not accept saved assertions, source matches, or caller-authored pass evidence.
The registry treats a skipped platform prerequisite as an unpassed active gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.gates.test_email_chat_m26 import (
    assert_chat_feedback_requires_current_owner_source_and_replays_shared_rule as profile_check,
)
from tests.gates.test_email_chat_m26 import (
    assert_chat_shared_style as shared_style_check,
)
from tests.gates.test_email_chat_m26 import (
    assert_selected_email_context as shared_context_check,
)
from tests.gates.test_email_experience_m26 import (
    assert_refresh_admits_one_durable_typed_task_without_owner_message as admission_check,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def apple_environment() -> dict[str, str]:
    if sys.platform != "darwin":
        pytest.skip("Native Email acceptance requires macOS/Xcode; this is not a passing gate.")
    developer = Path(os.environ.get("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer"))
    if not (developer / "usr/bin/xcodebuild").is_file():
        pytest.skip("Full Xcode is unavailable; native acceptance has not been verified.")
    return {**os.environ, "DEVELOPER_DIR": str(developer)}


def native_command(arguments: list[str], environment: dict[str, str], *, timeout: int = 240) -> str:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-16000:]
    return output


@pytest.fixture(scope="module")
def apple_core(apple_environment: dict[str, str]) -> str:
    return native_command(
        [
            "swift",
            "test",
            "--package-path",
            "clients/apple",
            "--filter",
            "ChatViewModelTests|EmailViewModelTests",
        ],
        apple_environment,
    )


def native_test_passed(output: str, name: str) -> None:
    assert f"Test {name}() passed" in output, f"Native test did not report passing: {name}"


async def test_email_shared_mode_state(apple_core: str) -> None:
    native_test_passed(
        apple_core, "testModeSwitchPreservesLiveChatAndUsesSameConnectionForEmailHandoff"
    )
    native_test_passed(
        apple_core, "testColdAndWarmEmailNotificationsRouteWithoutReplacingChatState"
    )
    native_test_passed(apple_core, "testUnchangedRefreshDoesNotReplaceOwnerDraftEdits")
    await shared_context_check()
    await profile_check()
    await shared_style_check()


async def test_email_foreground_admission(apple_core: str) -> None:
    native_test_passed(apple_core, "testNoRefreshAdmissionAfterEmailLeavesForeground")
    native_test_passed(apple_core, "testForegroundReturnStartsOneLoopAndVisibleCadenceRepeats")
    await admission_check()


def test_email_native_layouts(apple_environment: dict[str, str], tmp_path: Path) -> None:
    inventory = json.loads(
        native_command(["xcrun", "simctl", "list", "devices", "available", "-j"], apple_environment)
    )
    devices: dict[str, str] = {}
    for family in ("iPhone", "iPad"):
        candidates = [
            (tuple(int(part) for part in runtime.rsplit("iOS-", 1)[1].split("-")), device["udid"])
            for runtime, values in inventory["devices"].items()
            if "iOS-" in runtime
            for device in values
            if device["name"].startswith(family) and device["isAvailable"]
        ]
        if not candidates:
            pytest.skip(f"No {family} simulator is available; native layout gate is unverified.")
        devices[family] = max(candidates)[1]

    ios_cases = [
        "testEmailModePreservesAnUnsentChatMessage",
        "testEmailThreadFeedbackEditingAndExplicitSend",
        "testEmailLearningControlsShowCurrentState",
        "testEmailCompactTraitNavigationReturnsToSelectedInbox",
    ]
    for platform, destination, cases in [
        ("macos", "platform=macOS", ["testEmailModeAndExactDraftApprovalOnMac"]),
        ("iphone", f"platform=iOS Simulator,id={devices['iPhone']}", ios_cases),
        ("ipad", f"platform=iOS Simulator,id={devices['iPad']}", ios_cases),
    ]:
        bundle = tmp_path / f"{platform}.xcresult"
        native_command(
            [
                "xcodebuild",
                "test",
                "-quiet",
                "-project",
                "clients/apple/Veetbot.xcodeproj",
                "-scheme",
                "Veetbot",
                "-destination",
                destination,
                "-resultBundlePath",
                str(bundle),
                *[
                    f"-only-testing:VeetbotUITests/ConversationNavigationUITests/{case}"
                    for case in cases
                ],
                "CODE_SIGN_STYLE=Manual",
                "CODE_SIGN_IDENTITY=-",
                "CODE_SIGNING_REQUIRED=NO",
                "CODE_SIGN_ENTITLEMENTS=",
                "PROVISIONING_PROFILE_SPECIFIER=",
                "DEVELOPMENT_TEAM=",
            ],
            apple_environment,
            timeout=900,
        )
        summary = json.loads(
            native_command(
                ["xcrun", "xcresulttool", "get", "test-results", "summary", "--path", str(bundle)],
                apple_environment,
            )
        )
        assert summary["passedTests"] == len(cases), summary
        assert summary["failedTests"] == 0 and summary["skippedTests"] == 0, summary
        assert summary["expectedFailures"] == 0, summary
